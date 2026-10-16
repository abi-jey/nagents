import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { SessionRow } from "../features/sessions/SessionRow.js";
import { contextMenuPosition } from "../features/sessions/TreeActions.js";
import type { Session } from "../types.js";

function browser() {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document,
    innerWidth: 390, innerHeight: 420, IS_REACT_ACT_ENVIRONMENT: true });
  function toggle(element: HTMLElement, open: boolean) {
    element.toggleAttribute("data-popover-open", open);
    const event = new dom.window.Event("toggle");
    Object.defineProperty(event, "newState", { value: open ? "open" : "closed" });
    element.dispatchEvent(event);
  }
  dom.window.HTMLElement.prototype.showPopover = function () {
    for (const element of dom.window.document.querySelectorAll<HTMLElement>("[data-popover-open]"))
      if (element !== this) toggle(element, false);
    toggle(this, true);
  };
  dom.window.HTMLElement.prototype.hidePopover = function () { toggle(this, false); };
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const actions: string[] = [];
  const sessions: Session[] = [
    { id: "current", title: "Current", updated_at: "today" },
    { id: "other", title: "Other", updated_at: "today", active_run_id: "running" },
  ];
  return {
    dom, container, actions,
    async render(blocked = false) {
      await act(async () => root.render(createElement("div", null, ...sessions.map(session => createElement(SessionRow, {
        key: session.id, session, selected: session.id === "current", disabled: blocked, canDelete: !blocked, canFork: !blocked,
        select: (id: string) => actions.push(`select:${id}`), remove: (item: Session) => actions.push(`trash:${item.id}`),
        permanent: (item: Session) => actions.push(`delete:${item.id}`),
        rename: (item: Session) => actions.push(`rename:${item.id}`), fork: (item: Session) => actions.push(`fork:${item.id}`),
      })))));
    },
    row(id = "other") { return container.querySelector<HTMLElement>(`[data-session-id="${id}"]`)!; },
    async dispatch(element: Element, event: Event) { await act(async () => { element.dispatchEvent(event); }); },
    async cleanup() {
      await act(async () => root.unmount()); dom.window.close();
      for (const name of ["window", "document", "innerWidth", "innerHeight", "IS_REACT_ACT_ENVIRONMENT"])
        if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    },
  };
}

test("right-click opens deletion for a running chat without selecting it", async () => {
  const view = browser();
  try {
    await view.render();
    const row = view.row(), menu = row.querySelector<HTMLElement>('[role="menu"]')!;
    const event = new view.dom.window.MouseEvent("contextmenu", { bubbles: true, cancelable: true, button: 2, clientX: 380, clientY: 410 });
    await view.dispatch(row.querySelector(".session-title")!, event);
    assert.equal(event.defaultPrevented, true);
    assert.equal(menu.hasAttribute("data-popover-open"), true);
    assert.equal(row.querySelector(".session-more")!.getAttribute("aria-expanded"), "true");
    assert.equal(view.dom.window.document.activeElement?.textContent?.trim(), "Rename chat…");
    assert.equal(row.querySelector(".session")!.getAttribute("aria-current"), null);
    assert.equal(view.row("current").querySelector(".session")!.getAttribute("aria-current"), "page");
    assert.equal(menu.style.left, "170px"); assert.equal(menu.style.top, "348px");
    const items = [...menu.querySelectorAll<HTMLButtonElement>("button")];
    assert.equal(items.find(item => item.textContent?.includes("Fork"))!.disabled, true);
    assert.equal(items.find(item => item.textContent?.includes("Delete"))!.disabled, false);
    assert.deepEqual(view.actions, []);
    await act(async () => items[0].click());
    assert.deepEqual(view.actions, ["rename:other"]);
    assert.equal(menu.hasAttribute("data-popover-open"), false);
    assert.equal(view.dom.window.document.activeElement, row.querySelector(".session"));
  } finally { await view.cleanup(); }
});

test("keyboard context menu opens at the row and Escape returns focus without closing a containing drawer", async () => {
  const view = browser();
  let escaped = 0;
  view.dom.window.document.addEventListener("keydown", event => { if (event.key === "Escape") escaped++; });
  try {
    await view.render();
    const row = view.row("current"), origin = row.querySelector<HTMLButtonElement>(".session")!;
    for (const activation of [{ key: "F10", shiftKey: true }, { key: "ContextMenu" }]) {
      origin.focus();
      const event = new view.dom.window.KeyboardEvent("keydown", { ...activation, bubbles: true, cancelable: true });
      await view.dispatch(origin, event);
      assert.equal(event.defaultPrevented, true);
      const menu = row.querySelector<HTMLElement>('[role="menu"]')!;
      assert.equal(menu.hasAttribute("data-popover-open"), true);
      await view.dispatch(view.dom.window.document.activeElement!, new view.dom.window.KeyboardEvent("keydown", { key: "End", bubbles: true, cancelable: true }));
      assert.match(view.dom.window.document.activeElement!.textContent!, /Delete forever/);
      await view.dispatch(view.dom.window.document.activeElement!, new view.dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
      assert.equal(menu.hasAttribute("data-popover-open"), false);
      assert.equal(view.dom.window.document.activeElement, origin);
    }
    assert.equal(escaped, 0); assert.deepEqual(view.actions, []);
  } finally { await view.cleanup(); }
});

test("More actions remains available and light dismissal, scrolling and resize keep menu state synchronized", async () => {
  const view = browser();
  try {
    await view.render();
    const row = view.row(), button = row.querySelector<HTMLButtonElement>(".session-more")!;
    const menu = row.querySelector<HTMLElement>('[role="menu"]')!;
    await act(async () => button.click());
    assert.equal(button.getAttribute("aria-expanded"), "true");
    await view.dispatch(menu, new view.dom.window.Event("scroll"));
    assert.equal(button.getAttribute("aria-expanded"), "true");
    await act(async () => menu.hidePopover()); // Native outside-click light dismissal.
    assert.equal(button.getAttribute("aria-expanded"), "false");
    await act(async () => button.click());
    await view.dispatch(row, new view.dom.window.Event("scroll"));
    assert.equal(button.getAttribute("aria-expanded"), "false");
    await act(async () => button.click());
    await act(async () => { view.dom.window.dispatchEvent(new view.dom.window.Event("resize")); });
    assert.equal(button.getAttribute("aria-expanded"), "false");
    assert.deepEqual(view.actions, []);
  } finally { await view.cleanup(); }
});

test("disabled session actions leave the native context menu available", async () => {
  const view = browser();
  try {
    await view.render(true);
    const event = new view.dom.window.MouseEvent("contextmenu", { bubbles: true, cancelable: true, button: 2 });
    await view.dispatch(view.row(), event);
    assert.equal(event.defaultPrevented, false);
    assert.equal(view.container.querySelector("[data-popover-open]"), null);
    assert.deepEqual(view.actions, []);
  } finally { await view.cleanup(); }
});

test("context menu fits the viewport at every edge including short mobile screens", () => {
  for (const point of [{ x: 0, y: 0 }, { x: 1000, y: 1000 }, { x: 389, y: 419 }]) {
    const position = contextMenuPosition(point, 390, 420, 212, 232);
    assert.ok(position.left >= 8 && position.left + 212 <= 382);
    assert.ok(position.top >= 8 && position.top + 232 <= 412);
  }
});
