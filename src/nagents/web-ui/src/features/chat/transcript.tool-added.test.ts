import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import type { Snapshot, WireEvent } from "../../types.js";
import { Conversation } from "./Conversation.js";
import { applySnapshot } from "./liveTranscript.js";
import { appendEvent, fromHistory, groupTranscript, type Entry } from "./transcript.js";

const added: WireEvent = {
  event: "tool_added",
  history_id: "tool-added-17",
  text: "Tool added: weather_forecast",
};

function savedMessage(): Snapshot["history"][number] {
  return {
    role: "user",
    content: "Tool added: weather_forecast",
    name: "ngn_tools_added",
    tool_call_id: "",
    tool_calls: [],
    history_id: "tool-added-17",
  };
}

function ordinaryMessage(): Snapshot["history"][number] {
  return {
    role: "user",
    content: "Help me inspect the project.",
    name: "",
    tool_call_id: "",
    tool_calls: [],
    history_id: "user-4",
  };
}

function snapshot(history: Snapshot["history"], active = false): Snapshot {
  return {
    session_id: "root",
    activity_cursor: 17,
    sessions: [],
    retained_tasks: [],
    history,
    ...(active ? {
      active_run: {
        id: "active-run",
        session_id: "root",
        status: "running",
        events: [added],
      },
    } : {}),
  };
}

function render(entries: Entry[]): string {
  return renderToStaticMarkup(createElement(Conversation, {
    entries,
    sessionId: "root",
    demo: false,
    canSubmit: false,
    submit: () => assert.fail("No submit expected"),
  }));
}

function assertCompactToolAddedRow(entries: Entry[]): void {
  assert.deepEqual(groupTranscript(entries), [{ kind: "entry", entry: entries[0] }]);
  const dom = new JSDOM(render(entries));
  try {
    const rows = dom.window.document.querySelectorAll("article.entry.tool_change");
    assert.equal(rows.length, 1);
    assert.equal(rows[0]?.querySelectorAll(".tool-added-line").length, 1);
    assert.equal(rows[0]?.querySelector(".tool-added-line")?.textContent, "Tool added: weather_forecast");
    assert.equal(rows[0]?.querySelector(".entry-label"), null);
    assert.equal(rows[0]?.querySelector("details, summary, button"), null);
    assert.equal(dom.window.document.querySelector(".notice-group, .tools-group"), null);
  } finally {
    dom.window.close();
  }
}

test("tool-added rows stay one compact unlabeled entry through live, saved and checkpoint replay", () => {
  const live = appendEvent([], added);
  const duplicate = appendEvent(live, added);
  const saved = fromHistory(snapshot([savedMessage()]));
  const checkpoint = applySnapshot({ entries: live }, snapshot([savedMessage()], true));
  const coldCheckpoint = applySnapshot({ entries: [] }, snapshot([savedMessage()], true));
  const repeatedCheckpoint = applySnapshot(coldCheckpoint, snapshot([savedMessage()], true));

  for (const entries of [live, duplicate, saved, checkpoint.entries, coldCheckpoint.entries, repeatedCheckpoint.entries]) {
    assert.equal(entries.length, 1);
    assert.equal(entries[0]?.kind, "tool_change");
    assert.equal(entries[0]?.historyId, "tool-added-17");
    assertCompactToolAddedRow(entries);
  }
  assert.equal(checkpoint.entries[0]?.id, live[0]?.id, "Saved history and replay keep the observed row identity");
  assert.equal(repeatedCheckpoint.entries[0]?.id, coldCheckpoint.entries[0]?.id, "Cold replay IDs remain stable");
});

test("stale history checkpoints retain unseen tool-added facts without duplicating them", () => {
  const baseline = applySnapshot({ entries: [] }, snapshot([ordinaryMessage()]));
  const current = { ...baseline, entries: appendEvent(baseline.entries, added) };
  const stale = applySnapshot(current, snapshot([ordinaryMessage()]));
  const repeated = applySnapshot(stale, snapshot([ordinaryMessage()]));

  for (const state of [stale, repeated]) {
    const rows = state.entries.filter((entry) => entry.kind === "tool_change");
    assert.equal(rows.length, 1);
    assert.equal(rows[0]?.historyId, "tool-added-17");
    assert.equal(rows[0]?.text, "Tool added: weather_forecast");
  }
  assert.equal(
    repeated.entries.find((entry) => entry.kind === "tool_change")?.id,
    current.entries.find((entry) => entry.kind === "tool_change")?.id,
  );
});

test("different tool-added history IDs remain separate visible rows", () => {
  const first = appendEvent([], added);
  const second = appendEvent(first, {
    ...added,
    history_id: "tool-added-18",
    text: "Tool added: calendar_lookup",
  });
  assert.deepEqual(second.map((entry) => entry.text), ["Tool added: weather_forecast", "Tool added: calendar_lookup"]);
  assert.equal(groupTranscript(second).filter((item) => item.kind === "entry").length, 2);
});
