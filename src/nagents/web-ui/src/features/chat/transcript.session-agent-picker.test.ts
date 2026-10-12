import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { SessionAgentPicker } from "../sessions/SessionAgentPicker.js";
import type { SessionAgentSelection } from "../sessions/sessionAgent.js";

// Explicit fixture profiles and model IDs; no provider catalog or model calls.
function selection(): SessionAgentSelection {
  return {
    session_id: "fixture-conversation", revision: "fixture-revision", agent: "assistant", workspace_default: "assistant",
    effective: { agent: "assistant", model: "fixture-fast-model", provider: "Fixture OpenAI", provider_kind: "openai", mode: "build" },
    active: null, pending: false, locked_reason: "",
    profiles: [
      { name: "assistant", description: "General workspace work", model: "fixture-fast-model", provider: "Fixture OpenAI", provider_kind: "openai", mode: "build", disabled_reason: "" },
      { name: "reviewer", description: "Review changes without writing files", model: "fixture-review-model", provider: "Fixture Anthropic", provider_kind: "anthropic", mode: "reviewer", disabled_reason: "" },
      { name: "builder", description: "Write and test changes", model: "fixture-build-model", provider: "Fixture Gemini", provider_kind: "gemini", mode: "build", disabled_reason: "This workspace is locked to read-only profiles." },
    ],
  };
}

async function view(t: TestContext, data: SessionAgentSelection | undefined = selection(), choose: (name: string) => Promise<boolean> = async () => true) {
  const dom = new JSDOM("<button id='trigger'>Choose agent</button><div id='root'></div>");
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
  Object.assign(dom.window.HTMLElement.prototype, { attachEvent() {}, detachEvent() {} });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const root = createRoot(dom.window.document.getElementById("root")!);
  const trigger = dom.window.document.getElementById("trigger")!;
  trigger.focus();
  const calls: string[] = [];
  let dismissals = 0, refreshes = 0;
  let props = { selection: data, loading: !data, saving: false, error: "" };
  const render = async (updates: Partial<typeof props> = {}) => {
    props = { ...props, ...updates };
    await act(async () => root.render(createElement(SessionAgentPicker, {
      ...props, choose: async name => { calls.push(name); return choose(name); },
      refresh: () => { refreshes++; }, close: () => { dismissals++; root.render(null); },
    })));
  };
  t.after(async () => {
    await act(async () => root.unmount());
    dom.window.close();
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  });
  await render();
  const document = dom.window.document;
  return {
    dom, document, trigger, calls, render,
    dismissals: () => dismissals, refreshes: () => refreshes,
    option: (name: string) => [...document.querySelectorAll<HTMLButtonElement>(".session-agent-option")].find(button => button.querySelector("strong")?.textContent === name)!,
    async cancel() {
      const event = new dom.window.Event("cancel", { cancelable: true });
      await act(async () => document.querySelector("dialog")!.dispatchEvent(event));
      assert.equal(event.defaultPrevented, true);
    },
  };
}

test("conversation picker shows profile descriptions, provider models, default and disabled reasons", async t => {
  const ui = await view(t);
  const dialog = ui.document.querySelector("dialog")!;
  assert.equal(dialog.open, true);
  assert.equal(dialog.parentElement, ui.document.body);
  assert.equal(ui.document.getElementById(dialog.getAttribute("aria-labelledby")!)?.textContent, "Agent for this conversation");
  assert.equal(dialog.getAttribute("aria-modal"), "true");
  assert.equal(ui.document.activeElement?.tagName, "H2");
  assert.equal(ui.option("assistant").getAttribute("aria-pressed"), "true");
  assert.match(ui.option("assistant").textContent!, /Workspace default/);
  assert.match(ui.option("reviewer").textContent!, /Review changes without writing files/);
  assert.match(ui.option("reviewer").textContent!, /Fixture Anthropic · fixture-review-model/);
  assert.match(ui.option("reviewer").textContent!, /Read-only/);
  assert.equal(ui.option("builder").disabled, true);
  assert.match(ui.option("builder").textContent!, /locked to read-only profiles/);
  await act(async () => ui.option("builder").click());
  assert.deepEqual(ui.calls, []);
});

test("choosing a profile applies only its name, then closes and restores trigger focus", async t => {
  const ui = await view(t);
  await act(async () => ui.option("reviewer").click());
  assert.deepEqual(ui.calls, ["reviewer"]);
  assert.equal(ui.dismissals(), 1);
  assert.equal(ui.document.querySelector("dialog"), null);
  assert.equal(ui.document.activeElement, ui.trigger);
  assert.equal(ui.document.documentElement.style.overflow, "");
});

test("pending profile shows current and next-run identities without suggesting a mid-run switch", async t => {
  const data = selection();
  data.active = data.effective;
  data.effective = { agent: "reviewer", model: "fixture-review-model", provider: "Fixture Anthropic", provider_kind: "anthropic", mode: "reviewer" };
  data.agent = "reviewer"; data.pending = true;
  const ui = await view(t, data);
  const current = ui.document.querySelector(".session-agent-current")!.textContent!;
  assert.match(current, /Next runreviewer/);
  assert.match(current, /The current run continues as assistant/);
  assert.match(current, /reviewer applies from the next run/);
  assert.match(current, /Read-only restrictions apply now/);
  assert.equal(ui.option("reviewer").getAttribute("aria-pressed"), "true");
  assert.equal(ui.option("assistant").getAttribute("aria-pressed"), "false");
});

test("locked conversations retain profile information while disabling every change", async t => {
  const data = selection();
  data.locked_reason = "This conversation uses a saved agent design.";
  data.active = { ...data.effective, mode: "reviewer" };
  const ui = await view(t, data);
  assert.match(ui.document.querySelector("dialog")!.textContent!, /uses a saved agent design/);
  assert.match(ui.document.querySelector(".session-agent-current")!.textContent!, /current run is read-only/);
  for (const button of ui.document.querySelectorAll<HTMLButtonElement>(".session-agent-option")) assert.equal(button.disabled, true);
  await ui.cancel();
  assert.equal(ui.dismissals(), 1);
  assert.equal(ui.document.activeElement, ui.trigger);
});

test("an in-flight choice cannot submit twice or be dismissed; an unsuccessful save keeps the picker open", async t => {
  let complete: (value: boolean) => void = () => {};
  const response = new Promise<boolean>(resolve => { complete = resolve; });
  const ui = await view(t, selection(), async () => response);
  await act(async () => { ui.option("reviewer").click(); ui.option("reviewer").click(); });
  assert.deepEqual(ui.calls, ["reviewer"]);
  assert.equal(ui.option("assistant").disabled, true);
  assert.match(ui.document.querySelector(".session-agent-picker-status")!.textContent!, /Applying reviewer/);
  await ui.cancel();
  assert.equal(ui.dismissals(), 0);
  await act(async () => complete(false));
  assert.equal(ui.document.querySelector("dialog")!.open, true);
  assert.equal(ui.option("reviewer").disabled, false);
  await ui.render({ error: "The conversation changed. Refresh and choose again." });
  assert.match(ui.document.querySelector('[role="alert"]')!.textContent!, /conversation changed/);
  await act(async () => [...ui.document.querySelectorAll("button")].find(button => button.textContent === "Refresh")!.click());
  assert.equal(ui.refreshes(), 1);
});

test("a rejected save reports an error without closing or losing the selection", async t => {
  const ui = await view(t, selection(), async () => { throw new Error("Connection interrupted. Try again."); });
  await act(async () => ui.option("reviewer").click());
  assert.equal(ui.dismissals(), 0);
  assert.match(ui.document.querySelector('[role="alert"]')!.textContent!, /Connection interrupted/);
  assert.equal(ui.option("assistant").getAttribute("aria-pressed"), "true");
  assert.equal(ui.option("reviewer").disabled, false);
});

test("loading and failed discovery remain dismissible and offer refresh", async t => {
  const ui = await view(t);
  await ui.render({ selection: undefined, loading: true });
  assert.match(ui.document.querySelector("dialog")!.textContent!, /Loading agent profiles/);
  assert.equal(ui.document.querySelector(".session-agent-empty"), null);
  await ui.render({ loading: false, error: "Profiles could not be loaded." });
  assert.match(ui.document.querySelector('[role="alert"]')!.textContent!, /could not be loaded/);
  await act(async () => [...ui.document.querySelectorAll("button")].find(button => button.textContent === "Refresh")!.click());
  assert.equal(ui.refreshes(), 1);
  await ui.cancel();
  assert.equal(ui.dismissals(), 1);
});

test("a larger profile list can be filtered by provider or description without losing the selected choice", async t => {
  const data = selection();
  data.profiles.push(...Array.from({ length: 5 }, (_, index) => ({
    name: `fixture-extra-${index}`, description: "Additional fixture profile", model: "fixture-extra-model",
    provider: "Fixture local", provider_kind: "openai", mode: "build" as const, disabled_reason: "",
  })));
  const ui = await view(t, data);
  const input = ui.document.querySelector<HTMLInputElement>('input[type="search"]')!;
  const filter = async (value: string) => {
    await act(async () => { input.value = value; input.dispatchEvent(new ui.dom.window.Event("input", { bubbles: true })); });
  };
  await filter("anthropic");
  assert.equal(ui.document.querySelectorAll(".session-agent-option").length, 1);
  assert.equal(ui.option("reviewer").disabled, false);
  await filter("does not exist");
  assert.match(ui.document.querySelector(".session-agent-empty")!.textContent!, /No profiles match/);
  await filter("general workspace");
  assert.equal(ui.option("assistant").getAttribute("aria-pressed"), "true");
  assert.deepEqual(ui.calls, []);
});
