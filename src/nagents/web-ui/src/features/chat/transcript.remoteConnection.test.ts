import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { act, createElement, type ReactElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { RemoteConnectionChip } from "../team/RemoteConnectionChip.js";
import { remoteAuthLabel, type RemoteAuthentication } from "../team/RemoteStatus.js";
import { AgentRow, TeamTree } from "../team/TeamTree.js";
import type { TeamAgent } from "../team/model.js";

function agent(remote: boolean): TeamAgent {
  return { id: "child", name: "clear cedar", profile: "remote-desktop", parentId: "", prompt: "Inspect the machine",
    phase: "done", reason: "A long recorded response belongs in the conversation, not the status tooltip.",
    provider: remote ? "" : "openai", providerKind: remote ? "a2a" : "openai", model: remote ? "" : "gpt-6.1-sol",
    startedAt: 0, updatedAt: 0, canMessage: true, messageReason: "" };
}

function mounted(t: TestContext) {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost/" });
  const keys = ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT", "requestAnimationFrame", "cancelAnimationFrame"];
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: (callback: FrameRequestCallback) => { callback(0); return 0; }, cancelAnimationFrame() {} });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  t.after(async () => {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of keys) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  });
  return { container, render: (element: ReactElement) => act(async () => root.render(element)) };
}

test("connection chip identifies A2A and distinguishes every supported authentication state without visible auth prose", () => {
  const states: [RemoteAuthentication["state"], string, string][] = [
    ["signed_in", "ready", "Signed in"], ["sign_in_required", "attention", "Sign-in required"],
    ["token_ready", "ready", "Server token configured"], ["token_missing", "attention", "Server token missing"],
    ["not_required", "ready", "No sign-in required"], ["storage_error", "attention", "Credentials unavailable"],
    ["host_setup_required", "attention", "Sign-in setup required"],
  ];
  for (const [state, visual, description] of states) {
    const html = renderToStaticMarkup(createElement(RemoteConnectionChip, {
      authentication: { state, expires_at: 0, error_code: "" }, name: "desktop", activate() {},
    }));
    const doc = new JSDOM(html).window.document;
    const button = doc.querySelector("button")!;
    assert.equal(button.getAttribute("data-state"), visual);
    assert.ok(button.getAttribute("aria-label")?.includes(description));
    assert.ok(button.getAttribute("title")?.includes(description));
    assert.equal(button.textContent, "A2A·");
    assert.equal(button.querySelectorAll("button").length, 0);
  }
  const unknown = new JSDOM(renderToStaticMarkup(createElement(RemoteConnectionChip, { name: "desktop", activate() {} }))).window.document;
  assert.equal(unknown.querySelector("button")?.getAttribute("data-state"), "unknown");
  assert.equal(unknown.querySelector(".remote-connection-shackle"), null, "Unknown credentials never look unlocked");
});

test("an unrefreshable token changes to sign-in required at expiry without changing a completed task", async t => {
  const view = mounted(t);
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 1_000_000 });
  const value = { ...agent(true), authentication: { state: "signed_in" as const, expires_at: 1015, error_code: "" } };
  await view.render(createElement(AgentRow, { agent: value, selected: false, inspect() {}, settings() {} }));
  const row = view.container.querySelector("article")!;
  const chip = view.container.querySelector(".remote-connection-chip")!;
  assert.equal(chip.getAttribute("data-state"), "ready");
  const openShackle = chip.querySelector(".remote-connection-shackle")?.getAttribute("d");
  await act(async () => t.mock.timers.tick(5011));
  assert.equal(chip.getAttribute("data-state"), "attention");
  assert.notEqual(chip.querySelector(".remote-connection-shackle")?.getAttribute("d"), openShackle);
  assert.equal(view.container.querySelector("article"), row);
  assert.equal(view.container.querySelector(".team-state")?.textContent, "Done");
  assert.equal(view.container.querySelector(".team-state")?.getAttribute("title"), "Done");
  assert.equal(remoteAuthLabel({ ...value.authentication, refreshable: true }, 2000), "Signed in · auto refresh");
});

test("chip activation opens its definition connection; row inspection and conversation settings stay independent", async t => {
  const view = mounted(t);
  const opened: string[] = [], configured: string[] = [];
  const remote = { ...agent(true), phase: "running" as const,
    authentication: { state: "sign_in_required" as const, expires_at: 0, error_code: "" } };
  const root = { ...agent(false), id: "", name: "assistant", profile: "assistant", phase: "ready" as const };
  const profiles = [{ id: remote.profile, name: "Remote desktop", description: "Read-only checks", provider: "a2a", model: "", used: true, available: true }];
  await view.render(createElement(TeamTree, { agents: [root, remote], profiles, selected: "", inspect: id => opened.push(id), configure: id => configured.push(id), arrivals: {}, error: "", refresh() {} }));
  const card = view.container.querySelector('[data-agent-id="child"]')!;
  const group = view.container.querySelector<HTMLElement>('.team-definition-group[data-parent-id=""][data-profile="remote-desktop"]')!;
  assert.equal(group.querySelectorAll(".remote-connection-chip").length, 1);
  assert.equal(card.querySelector(".remote-connection-chip"), null, "a grouped remote definition owns the single connection chip");
  await act(async () => group.querySelector<HTMLButtonElement>(".remote-connection-chip")!.click());
  assert.deepEqual(configured, ["profile:remote-desktop"]);
  assert.equal(opened.length, 0);
  await act(async () => card.querySelector<HTMLButtonElement>(".team-agent-open")!.click());
  assert.deepEqual(opened, ["child"]);
  await act(async () => card.querySelector<HTMLButtonElement>(".team-agent-settings")!.click());
  assert.deepEqual(configured, ["profile:remote-desktop", "child"]);
  await view.render(createElement(TeamTree, { agents: [root, remote], profiles: [], selected: "", inspect: id => opened.push(id), configure: id => configured.push(id), arrivals: {}, error: "", refresh() {} }));
  const retainedGroup = view.container.querySelector<HTMLElement>('.team-definition-group[data-parent-id=""][data-profile="remote-desktop"]')!;
  await act(async () => retainedGroup.querySelector<HTMLButtonElement>(".remote-connection-chip")!.click());
  assert.equal(configured.at(-1), "child", "A removed definition falls back to this conversation instead of another connection");
});

test("native row content and controls stay intact while A2A rows omit fake model/context and result tooltips", () => {
  const render = (value: TeamAgent) => new JSDOM(renderToStaticMarkup(createElement(AgentRow, { agent: value, selected: false, inspect() {}, settings() {} }))).window.document;
  const native = render(agent(false));
  assert.equal(native.querySelector(".remote-connection-chip"), null);
  assert.equal(native.querySelector(".team-agent-model")?.textContent, "openai · gpt-6.1-sol");
  assert.equal(native.querySelector(".team-agent-context")?.textContent, "Context unavailable");
  assert.ok(native.querySelector(".provider-logo.has-brand img"));
  const remote = render(agent(true));
  assert.equal(remote.querySelector(".team-agent-model"), null);
  assert.equal(remote.querySelector(".team-agent-context"), null);
  assert.equal(remote.querySelector(".team-agent-meter"), null);
  assert.equal(remote.querySelector(".team-agent-heading .team-agent-definition")?.textContent, "remote-desktop");
  assert.equal(remote.querySelector(".team-agent-heading .team-agent-instance")?.textContent, "clear cedar");
  assert.equal(remote.querySelector(".team-state")?.getAttribute("title"), "Done");
  assert.equal(remote.querySelector("button button"), null);
});
