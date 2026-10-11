import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, useState } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { WorkspaceInfoDialog } from "../features/sessions/WorkspaceInfoDialog.js";

function browser() {
  const dom = new JSDOM("<button id='trigger'>Workspace</button><div id='root'></div>", { url: "http://localhost" });
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, {
    window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  return { dom, container, root, async cleanup() {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  } };
}

const connection = { token: "workspace-token", agent: "assistant", provider: "openai", model: "gpt-6.1-sol", connected: true };
const tools = {
  agents: { assistant: { schedule_wakeup: false, shell: false } }, profiles: [{ id: "assistant", mode: "build" }],
  tools: [{ name: "read_file" }, { name: "schedule_wakeup" }, { name: "shell" }],
  mcp_diagnostics: { status: "retained", configured_servers: 2, connected_servers: 1, error: "private diagnostic details" },
};
const props = {
  workspace: "/state/workspace", name: "workspace", demo: false, count: 3, running: 1, connection,
  close: () => {}, settings: () => {}, tools: () => {}, settingsDisabled: false, toolsDisabled: false,
};

test("workspace overview uses authenticated tool selections and explains a retained MCP snapshot", async t => {
  const view = browser();
  const calls: string[] = [];
  t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    calls.push(input);
    assert.equal(input, "/api/tools"); assert.equal(init.method, "GET");
    assert.equal((init.headers as Record<string, string>)["X-Ngn-Token"], connection.token);
    assert.equal(init.mode, "same-origin");
    return Response.json(tools);
  });
  try {
    await act(async () => view.root.render(createElement(WorkspaceInfoDialog, props)));
    assert.equal(view.dom.window.document.activeElement?.id, "workspace-dialog-title");
    for (const value of ["Connected to ngn", "/state/workspace", "assistant", "openai", "gpt-6.1-sol", "1 running", "1 of 2 MCP servers connected", "Previous configuration; reload failed"])
      assert.ok(view.container.textContent?.includes(value), value);
    const count = view.container.querySelectorAll(".workspace-resources strong");
    assert.equal(count[0].textContent, "3"); assert.equal(count[1].textContent, "1 / 3", "Counts use the current agent's tool selections");
    assert.doesNotMatch(view.container.textContent!, /Live provider|private diagnostic|workspace-token/);
    assert.deepEqual(calls, ["/api/tools"]);
  } finally { await view.cleanup(); }
});

test("workspace actions close the overview before opening the target and preserve disabled actions", async () => {
  const view = browser(), actions: string[] = [];
  function Harness() {
    const [open, setOpen] = useState(true);
    return open && createElement(WorkspaceInfoDialog, { ...props, connection: undefined, toolsDisabled: true,
      close: () => { actions.push("close"); setOpen(false); }, settings: () => { actions.push("settings"); },
      tools: () => assert.fail("Disabled actions cannot run"),
    });
  }
  try {
    view.dom.window.document.getElementById("trigger")!.focus();
    await act(async () => view.root.render(createElement(Harness)));
    const buttons = [...view.container.querySelectorAll<HTMLButtonElement>("footer button")];
    assert.equal(buttons[0].disabled, true); assert.equal(buttons[0].textContent, "Manage tools");
    await act(async () => buttons[1].click());
    assert.deepEqual(actions, ["close", "settings"]);
    assert.equal(view.container.querySelector("dialog"), null);
    assert.equal(view.dom.window.document.activeElement?.id, "trigger");
  } finally { await view.cleanup(); }
});

test("workspace tool failures do not masquerade as zero tools and can be retried", async t => {
  const view = browser(); let attempts = 0;
  t.mock.method(globalThis, "fetch", async () => {
    if (++attempts === 1) throw new Error("network failure");
    return Response.json({ ...tools, profiles: [{ id: "assistant", mode: "reviewer" }],
      mcp_diagnostics: { ...tools.mcp_diagnostics, status: "disabled", connected_servers: 0, error: "" } });
  });
  try {
    await act(async () => view.root.render(createElement(WorkspaceInfoDialog, props)));
    assert.match(view.container.textContent!, /Could not load tools/);
    assert.equal(view.container.querySelectorAll(".workspace-resources strong").length, 1);
    await act(async () => view.container.querySelector<HTMLButtonElement>(".workspace-retry")!.click());
    assert.match(view.container.textContent!, /Read-only restrictions apply/);
    assert.match(view.container.textContent!, /Startup disabled/);
    assert.doesNotMatch(view.container.textContent!, /Could not load tools|Retry tools/);
  } finally { await view.cleanup(); }
});

test("workspace requests abort on close or changed credentials and ignore late results", async t => {
  const view = browser();
  const pending: { signal: AbortSignal; resolve: (response: Response) => void }[] = [];
  t.mock.method(globalThis, "fetch", (_input: string, init: RequestInit) => new Promise<Response>(resolve => {
    pending.push({ signal: init.signal as AbortSignal, resolve });
  }));
  try {
    await act(async () => view.root.render(createElement(WorkspaceInfoDialog, props)));
    await act(async () => view.root.render(createElement(WorkspaceInfoDialog, { ...props, connection: { ...connection, token: "new-token" } })));
    assert.equal(pending[0].signal.aborted, true);
    await act(async () => pending[0].resolve(Response.json(tools)));
    assert.match(view.container.textContent!, /Loading…/);
    assert.doesNotMatch(view.container.textContent!, /MCP servers connected/);
    await act(async () => view.root.render(null));
    assert.equal(pending[1].signal.aborted, true);
    await act(async () => pending[1].resolve(Response.json(tools)));
    assert.equal(view.container.childElementCount, 0);
  } finally { await view.cleanup(); }
});

test("an unauthenticated workspace does not query tools or claim provider connectivity", async t => {
  const view = browser();
  t.mock.method(globalThis, "fetch", () => assert.fail("Unauthenticated inspection must not fetch"));
  try {
    await act(async () => view.root.render(createElement(WorkspaceInfoDialog, { ...props, connection: undefined, running: 0 })));
    assert.match(view.container.textContent!, /Connecting to ngn…/);
    assert.match(view.container.textContent!, /Reconnect to load tools/);
    assert.match(view.container.textContent!, /None running/);
    assert.doesNotMatch(view.container.textContent!, /Connected to ngn|Live provider/);
  } finally { await view.cleanup(); }
});
