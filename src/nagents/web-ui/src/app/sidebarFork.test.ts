import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, useState } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { WorkspaceSidebar } from "./WorkspaceSidebar.js";
import type { Client } from "./useClient.js";

/** Missing dependencies fail immediately instead of silently passing in a UI stub. */
function stub<T extends object>(values: Partial<T>): T {
  return new Proxy(values, { get(target, key) {
    if (!(key in target)) throw new Error(`Unexpected sidebar dependency: ${String(key)}`);
    return Reflect.get(target, key);
  } }) as T;
}

for (const outcome of ["success", "failure", "rename"] as const) {
  test(`sidebar ${outcome}: fork is immediate and unnamed; rename keeps its own dialog`, async () => {
    const dom = new JSDOM("<div id='root'></div><footer class='composer-area'><textarea id='composer'></textarea></footer>",
      { url: "http://localhost" });
    const previous = Object.getOwnPropertyDescriptors(globalThis);
    const frames: FrameRequestCallback[] = [];
    const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
      localStorage: dom.window.localStorage, innerWidth: 390, innerHeight: 844, IS_REACT_ACT_ENVIRONMENT: true,
      requestAnimationFrame: (callback: FrameRequestCallback) => frames.push(callback) };
    for (const [name, value] of Object.entries(globals)) Object.defineProperty(globalThis, name, { configurable: true, value });
    Object.defineProperty(dom.window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });
    Object.assign(dom.window.HTMLElement.prototype, { attachEvent() {}, detachEvent() {} });
    dom.window.HTMLElement.prototype.showPopover = function () { this.setAttribute("data-open", ""); };
    dom.window.HTMLElement.prototype.hidePopover = function () { this.removeAttribute("data-open"); };
    dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
    dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
    const root = createRoot(document.getElementById("root")!);
    const composer = { current: document.getElementById("composer") as HTMLTextAreaElement };
    const calls: unknown[][] = [], renamed: string[][] = [];
    let finish!: (value: boolean) => void, closed = 0;
    const pending = new Promise<boolean>(resolve => { finish = resolve; });
    const source = { id: "source", title: "Original", updated_at: "today" };
    const client = stub<Client>({
      sessions: stub<Client["sessions"]>({ config: undefined, sessions: [source], sessionId: source.id }),
      available: { navigate: true, create: true, submit: true, settings: true, tools: true, channels: true, designer: true, live: true, trash: true },
      busy: false, error: "", deletion: { pending: false, error: "" },
      deletionController: stub<Client["deletionController"]>({ show() {} }),
      trash: { open: false, pending: "", loading: false, error: "", draftDays: "30" },
      trashController: stub<Client["trashController"]>({ show() {} }),
      settings: stub<Client["settings"]>({ show() {} }), channels: stub<Client["channels"]>({ show() {} }),
      showTools() {}, showDesigner() {}, canDeleteSession: () => true, canForkSession: () => true,
      dismissError: () => { client.error = ""; },
      forkSession: async (...args) => { calls.push(args); return pending; },
      renameSession: async (...args) => { renamed.push(args); return true; },
    });
    function Probe() {
      const [open, setOpen] = useState(true);
      return createElement(WorkspaceSidebar, { client, composer, open, collapsed: false,
        close: () => { closed++; setOpen(false); }, select: async () => {}, toggleCollapsed() {} });
    }
    async function click(selector: string) {
      const button = document.querySelector<HTMLButtonElement>(selector);
      assert.ok(button, selector);
      await act(async () => button.click());
    }
    try {
      await act(async () => root.render(createElement(Probe)));
      await click('[aria-label="More actions: Original"]');
      const items = [...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
      const item = items.find(button => button.textContent?.trim() === (outcome === "rename" ? "Rename chat…" : "Fork chat"));
      assert.ok(item);
      await act(async () => item.click());
      if (outcome === "rename") {
        const dialog = document.querySelector("dialog[open]");
        assert.ok(dialog);
        const input = dialog.querySelector("input")!;
        assert.equal(input.value, source.title); assert.equal(input.required, true);
        await act(async () => dialog.querySelector("form")!.dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true })));
        assert.deepEqual(renamed, [[source.id, source.title]]);
        assert.deepEqual(calls, []); assert.equal(closed, 0);
        assert.equal(document.querySelector("dialog[open]"), null);
      } else {
        assert.deepEqual(calls, [[source.id]], "No title argument or confirmation dialog is required");
        assert.equal(document.querySelector("dialog"), null);
        assert.equal(document.querySelector(".session-menu[data-open]"), null);
        assert.equal(closed, 0, "Keep the source drawer until the fork succeeds");
        assert.ok(document.querySelector("#session-navigation.open"));
        client.error = outcome === "failure" ? "Source chat has queued work. Finish it before forking." : "";
        await act(async () => { finish(outcome === "success"); await pending; });
        for (const frame of frames.splice(0)) frame(0);
        if (outcome === "success") {
          assert.equal(closed, 1); assert.equal(document.querySelector("#session-navigation.open"), null);
          assert.equal(document.activeElement, composer.current);
          assert.equal(document.querySelector('[role="alert"]'), null);
        } else {
          assert.equal(closed, 0); assert.ok(document.querySelector("#session-navigation.open"));
          assert.match(document.querySelector('#session-navigation [role="alert"]')?.textContent || "", /queued work/);
          assert.notEqual(document.activeElement, composer.current);
          await click('[aria-label="Dismiss fork error"]');
          assert.equal(document.querySelector('[role="alert"]'), null);
          assert.equal(client.error, "");
        }
      }
    } finally {
      await act(async () => root.unmount()); dom.window.close();
      for (const name of Object.keys(globals))
        if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  });
}
