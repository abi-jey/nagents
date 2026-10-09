import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, type ComponentProps } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { Composer } from "./Composer.js";
import { commandIntent, commandMatches } from "./commands.js";

// Commands are recognized before normal message admission; unknown slash input
// remains a draft, rather than being interpreted by a model as an instruction.
test("command parsing handles aliases, arguments, and ordinary text explicitly", () => {
  assert.equal(commandIntent("please use /login"), undefined);
  assert.deepEqual(commandIntent(" /login \n"), { name: "login", argument: "" });
  assert.deepEqual(commandIntent("/"), { name: "help", argument: "" });
  assert.deepEqual(commandIntent("/resume ngn-123"), { name: "sessions", argument: "ngn-123" });
  assert.deepEqual(commandIntent("/model vendor/model-id"), { name: "model", argument: "vendor/model-id" });
  assert.throws(() => commandIntent("/unknown"), /Unknown browser command/);
  assert.throws(() => commandIntent("/login\nplease send this too"), /Usage: \/login/);
  assert.throws(() => commandIntent("/compact now"), /Usage: \/compact/);
  assert.throws(() => commandIntent("/quit"), /Unknown browser command/);
  assert.deepEqual(commandMatches("/log").map(command => command.name), ["login"]);
  assert.equal(commandMatches("/login ").length, 0);
  assert.equal(commandMatches("hello /log").length, 0);
});

test("slash picker handles arrows, completion, exact commands, Escape and composition without sending a prompt", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true,
    ResizeObserver: class { observe() {} disconnect() {} } });
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  let value = "/", submitted = 0;
  const props: ComponentProps<typeof Composer> = { inputRef: null, prompt: value,
    setPrompt: text => { value = text; }, demo: false, disabled: false, canSubmit: true, running: false,
    submit: () => { submitted++; }, cancel: () => {} };
  const render = () => act(async () => root.render(createElement(Composer, { ...props, prompt: value })));
  const key = async (key: string, options: KeyboardEventInit = {}) => act(async () => {
    container.querySelector("textarea")!.focus();
    container.querySelector("textarea")!.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...options }));
  });
  try {
    await render();
    assert.equal(container.querySelectorAll('[role="option"]').length, commandMatches("/").length);
    await key("ArrowDown");
    assert.equal(container.querySelector('[aria-selected="true"] code')!.textContent, "/login");
    await key("Enter");
    assert.equal(value, "/login "); assert.equal(submitted, 0);
    await render(); assert.equal(container.querySelector('[role="listbox"]'), null);
    await key("Enter"); assert.equal(submitted, 1);
    value = "/login"; await render();
    await key("Enter", { isComposing: true }); assert.equal(submitted, 1);
    await key("Enter"); assert.equal(submitted, 2, "An exact command executes immediately");
    value = "/mod"; await render();
    await key("Tab"); assert.equal(value, "/model "); assert.equal(submitted, 2);
    value = "/"; await render();
    await key("Escape"); assert.equal(container.querySelector('[role="listbox"]'), null);
    value = "/lo"; await render();
    await act(async () => container.querySelector<HTMLButtonElement>('[role="option"]')!.click());
    assert.equal(value, "/login ");
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT", "ResizeObserver"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
