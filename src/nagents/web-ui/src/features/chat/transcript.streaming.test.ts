import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import type { WireEvent } from "../../types.js";
import { Conversation } from "./Conversation.js";
import { LiveSessions } from "./liveTranscript.js";

test("live output is visible before completion and retains its reading surface through replay and terminal events", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    for (const outcome of ["completed", "error", "cancelled", "disconnected"] as const) {
      const cache = new LiveSessions();
      const records: WireEvent[] = [];
      let cursor = 0;
      const receive = (event: WireEvent) => {
        const record = { ...event, run_id: outcome, call_id: "call" };
        records.push(record);
        cache.receive({ type: "event", session_id: "root", epoch: "one", cursor: ++cursor, record });
      };
      const render = () => act(async () => root.render(createElement(Conversation, {
        key: outcome, entries: cache.get("root").entries, sessionId: "root", demo: false, canSubmit: false, submit() {},
      })));
      receive({ event: "run_started" });
      receive({ event: "tool_call", name: "shell", arguments: { command: "slow command" } });
      receive({ event: "tool_execution_started", name: "shell" });
      await render();
      const tool = container.querySelector<HTMLDetailsElement>(".tool-record")!;
      assert.equal(tool.open, false, "Starting alone does not expand the tool");
      if (outcome === "completed") {
        await act(async () => {
          tool.open = true;
          tool.dispatchEvent(new dom.window.Event("toggle"));
        });
        assert.equal(tool.querySelector<HTMLElement>('[data-panel="inputs"]')!.hidden, false);
      }
      const focused = dom.window.document.activeElement;
      receive({ event: "tool_output", text: "first line\n" });
      await render();
      const stream = tool.querySelector<HTMLElement>('[data-panel="stream"]')!;
      const reading = stream.querySelector("pre")!;
      const code = reading.querySelector("code")!;
      assert.equal(tool.open, true, "The first chunk is visible without opening the card");
      assert.equal(stream.hidden, false, "Opening early does not count as explicitly choosing Inputs");
      assert.equal(reading.textContent, "first line\n");
      assert.equal(tool.dataset.busy, "true");
      assert.equal(cache.get("root").entries.find(entry => entry.kind === "tool")?.result, undefined);
      assert.equal(dom.window.document.activeElement, focused, "Streaming never moves focus");

      reading.scrollTop = 37;
      receive({ event: "tool_output", text: "middle\n".repeat(6000) + "latest line\n" });
      await render();
      assert.equal(stream.querySelector("pre"), reading);
      assert.equal(reading.querySelector("code"), code);
      assert.equal(reading.scrollTop, 37);
      assert.ok(reading.textContent!.startsWith("first line\n"));
      assert.ok(reading.textContent!.endsWith("latest line\n"));
      assert.ok(reading.textContent!.length < 33_000, "Live rendering retains the existing display cap and tail");

      cache.receive({ type: "snapshot", session_id: "root", epoch: "one", cursor, snapshot: {
        session_id: "root", sessions: [], history: [], retained_tasks: [],
        active_run: { id: outcome, status: "running", events: records },
      } });
      await render();
      assert.equal(container.querySelector(".tool-record"), tool);
      assert.equal(stream.querySelector("pre"), reading);
      assert.equal(reading.scrollTop, 37);
      const output = cache.get("root").entries.find(entry => entry.kind === "tool")!.text;
      assert.equal(output, "first line\n" + "middle\n".repeat(6000) + "latest line\n", "Replay does not duplicate chunks");

      if (outcome === "completed" || outcome === "error") receive({
        event: "tool_result", result: { output, exit_code: outcome === "error" ? 7 : 0, timed_out: false, truncated: false },
        error: outcome === "error" ? "Command failed" : "",
      });
      else receive(outcome === "cancelled" ? { event: "run_finished", status: "cancelled" } : { event: "client_disconnected" });
      await render();
      assert.equal(tool.open, true);
      assert.equal(stream.hidden, false, "A distinct final result never replaces the live output tab");
      assert.equal(stream.querySelector("pre"), reading);
      assert.equal(reading.querySelector("code"), code);
      assert.equal(reading.scrollTop, 37);
      assert.ok(reading.textContent!.endsWith("latest line\n"));
      assert.equal(tool.dataset.busy, undefined);
      assert.equal(tool.querySelector(".execution-state")!.textContent,
        { completed: "Completed", error: "Error", cancelled: "Cancelled", disconnected: "Disconnected" }[outcome]);
      if (outcome === "error") assert.match(tool.querySelector(".tool-error")!.textContent!, /Command failed/);
    }
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});

test("streaming respects explicit tab and closed-card choices while saved output starts collapsed", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    for (const choice of ["Inputs", "Details", "closed"]) {
      const cache = new LiveSessions();
      let cursor = 0;
      const receive = (record: WireEvent) => cache.receive({ type: "event", session_id: "root", epoch: "one", cursor: ++cursor,
        record: { ...record, run_id: choice, call_id: "call" } });
      const render = () => act(async () => root.render(createElement(Conversation, {
        key: choice, entries: cache.get("root").entries, sessionId: "root", demo: false, canSubmit: false, submit() {},
      })));
      receive({ event: "tool_call", name: "run_shell_command", arguments: { command: "slow command" } });
      await render();
      const tool = container.querySelector<HTMLDetailsElement>(".tool-record")!;
      await act(async () => {
        tool.open = true;
        tool.dispatchEvent(new dom.window.Event("toggle"));
      });
      await act(async () => {
        if (choice === "closed") {
          tool.open = false;
          tool.dispatchEvent(new dom.window.Event("toggle"));
        } else [...tool.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(tab => tab.textContent === choice)!.click();
      });
      receive({ event: "tool_output", text: "partial output" });
      await render();
      assert.equal(tool.open, choice !== "closed");
      if (choice !== "closed") assert.equal(tool.querySelector('[aria-selected="true"]')!.textContent, choice);
      receive({ event: "tool_result", result: "final output" });
      await render();
      assert.equal(tool.open, choice !== "closed");
      if (choice !== "closed") assert.equal(tool.querySelector('[aria-selected="true"]')!.textContent, choice);
    }

    await act(async () => root.render(createElement(Conversation, {
      key: "saved", entries: [{ id: "saved", kind: "tool", title: "shell", text: "old output", result: "saved result", state: "Recorded result" }],
      sessionId: "root", demo: false, canSubmit: false, submit() {},
    })));
    assert.equal(container.querySelector<HTMLDetailsElement>(".tool-record")!.open, false);
    assert.equal(container.querySelector('[aria-selected="true"]')!.textContent, "Result");
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
