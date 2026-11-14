import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, type ComponentProps } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { Composer } from "./Composer.js";

for (const touch of [true, false]) test(`composer delivery actions on ${touch ? "touch" : "desktop"}`, async () => {
  const dom = new JSDOM("<div id='root'></div>");
  Object.assign(dom.window.HTMLElement.prototype, { attachEvent() {}, detachEvent() {} });
  Object.assign(dom.window, { matchMedia: () => ({ matches: touch, addEventListener() {}, removeEventListener() {} }) });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true,
    ResizeObserver: class { observe() {} disconnect() {} } });
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  const sent: (string | undefined)[] = [];
  let stopped = 0;
  const props: ComponentProps<typeof Composer> = { inputRef: null, prompt: "change direction", setPrompt() {},
    demo: false, disabled: false, canSubmit: true, running: true,
    submit: delivery => { sent.push(delivery); }, cancel: () => { stopped++; } };
  const render = () => act(async () => root.render(createElement(Composer, props)));
  try {
    await render();
    const input = container.querySelector("textarea")!;
    const enter = new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    await act(async () => { input.focus(); input.dispatchEvent(enter); });
    assert.equal(enter.defaultPrevented, !touch);
    assert.deepEqual(sent, touch ? [] : [undefined]);
    assert.equal(input.getAttribute("enterkeyhint"), touch ? "enter" : "send");
    if (touch) {
      const queue = container.querySelector<HTMLButtonElement>(".composer-queue")!;
      assert.equal(queue.textContent, "Queue");
      const down = new dom.window.MouseEvent("mousedown", { bubbles: true, cancelable: true });
      queue.dispatchEvent(down);
      assert.equal(down.defaultPrevented, true);
      assert.equal(dom.window.document.activeElement, input);
      await act(async () => { queue.click(); });
      await act(async () => { container.querySelector<HTMLButtonElement>('button[type="submit"]')!.click(); });
      assert.deepEqual(sent, ["queue", "steer"]);
      assert.equal(stopped, 0, "Neither delivery action cancels current work");
      props.submitting = true; await render();
      assert.equal(container.querySelector<HTMLButtonElement>(".composer-queue")!.disabled, true);
      assert.equal(container.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled, true);
      props.submitting = false; props.running = false; await render();
      assert.equal(container.querySelector(".composer-queue"), null);
      assert.equal(container.querySelector('button[type="submit"]')!.textContent, "Send");
      props.running = true; props.deliveryEnabled = false; await render();
      assert.equal(container.querySelector(".composer-queue"), null);
      props.deliveryEnabled = true; props.prompt = "/mod"; await render();
      const returnInSuggestions = new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
      await act(async () => { input.dispatchEvent(returnInSuggestions); });
      assert.equal(returnInSuggestions.defaultPrevented, false, "Touch Return must not select a command or send");
      assert.deepEqual(sent, ["queue", "steer"]);
      assert.equal(container.querySelector(".composer-queue"), null, "Commands use their existing dispatch path");
      assert.ok(container.querySelector("form")!.style.getPropertyValue("--composer-menu-space"), "Touch suggestions are constrained above the keyboard");
    } else assert.equal(container.querySelector(".composer-queue"), null);
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT", "ResizeObserver"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
