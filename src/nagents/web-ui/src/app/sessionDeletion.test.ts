import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { SessionRow } from "../features/sessions/SessionRow.js";

test("a running session can be trashed while another mutation still disables deletion", async () => {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true };
  for (const [name, value] of Object.entries(globals)) Object.defineProperty(globalThis, name, { configurable: true, value });
  const root = createRoot(document.getElementById("root")!);
  const removed: string[] = [];
  const session = { id: "active-root", title: "Working chat", updated_at: "today", active_run_id: "running" };
  const render = async (canDelete: boolean) => act(async () => root.render(createElement(SessionRow, {
    session, selected: true, disabled: false, canDelete, canFork: false,
    select() {}, permanent() {}, remove: item => { removed.push(item.id); },
  })));
  try {
    await render(true);
    const button = document.querySelector<HTMLButtonElement>('[aria-label="Move to Trash: Working chat"]')!;
    assert.equal(button.disabled, false);
    assert.deepEqual(removed, []);
    await act(async () => button.click());
    assert.deepEqual(removed, [session.id]);
    await render(false);
    assert.equal(button.disabled, true);
    await act(async () => button.click());
    assert.deepEqual(removed, [session.id]);
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const name of Object.keys(globals))
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
  }
});
