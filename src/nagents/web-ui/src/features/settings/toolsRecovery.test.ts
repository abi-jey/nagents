import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { ToolsDialog } from "../tools/ToolsDialog.js";
import type { McpDiagnostics } from "../tools/McpDiagnostics.js";

function browser() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document,
    HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const root = createRoot(dom.window.document.getElementById("root")!);
  return { dom, root, async cleanup() {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    }
  } };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>(done => { resolve = done; }), resolve };
}

const failed: McpDiagnostics = {
  status: "partial", configured_servers: 2, connected_servers: 1, registered_tools: 1, advertised_tools: 1,
  effective_source: "trusted configuration", sources: [], ignored_configs: [], error: "",
  failed_servers: [{ name: "broken", stage: "mcp_start", category: "missing_environment",
    message: "Configure the required server environment variable.", retry_pending: false }],
};
function catalog(diagnostics = failed, agents: Record<string, Record<string, boolean>> = {}, revision = "first") {
  return {
    path: ".ngn/tools.yaml", revision, active_agent: "assistant", agents,
    profiles: [{ id: "assistant", mode: "build" }], tool_approvals: [], mcp_diagnostics: diagnostics,
    tools: [{ name: "mcp__healthy__read", description: "Read a public fixture", category: "MCP",
      parameters: {}, builtin: false, reviewer_allowed: false }],
  };
}
const recovered: McpDiagnostics = { ...failed, status: "loaded", connected_servers: 2, failed_servers: [] };
const button = (label: string) => [...document.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent === label)!;

test("an MCP retry does not lock tool preferences or replace their saved selections", async t => {
  const view = browser(), retry = deferred<Response>();
  const writes: { path: string; body: object }[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    assert.equal(init.mode, "same-origin");
    assert.equal((init.headers as Record<string, string>)["X-Ngn-Token"], "token");
    if (init.method === "GET") return Response.json(catalog());
    const body = JSON.parse(String(init.body)) as { agents: Record<string, Record<string, boolean>> };
    writes.push({ path, body });
    if (path === "/api/tools/mcp/retry") return retry.promise;
    assert.equal(path, "/api/tools");
    return Response.json(catalog(failed, body.agents, "saved"));
  });
  try {
    await act(async () => view.root.render(createElement(ToolsDialog, { token: "token", blocked: false, close: () => {} })));
    await act(async () => button("Disable visible").click());
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Retry broken"]')!.click());
    assert.equal(document.querySelector('[aria-label="Retry broken"]')?.getAttribute("aria-busy"), "true");
    assert.equal(button("Save tools").disabled, false, "The MCP connection does not lock a preference save");
    assert.equal(button("Refresh").disabled, false);
    await act(async () => button("Save tools").click());
    assert.deepEqual(writes, [
      { path: "/api/tools/mcp/retry", body: { server: "broken" } },
      { path: "/api/tools", body: { revision: "first", agents: { assistant: { mcp__healthy__read: false } } } },
    ]);
    await act(async () => retry.resolve(Response.json({ ...catalog(recovered), mcp_retry: { server: "broken", status: "completed" } })));
    assert.equal(document.querySelector<HTMLInputElement>('[role="switch"]')?.checked, false);
    assert.match(document.body.textContent!, /broken connected/);
    assert.equal(button("Save tools").disabled, true, "Recovery does not undo a successful save or create a false dirty state");
  } finally { retry.resolve(Response.json(catalog())); await view.cleanup(); }
});

test("queued retries update from polling without discarding a draft or making duplicate writes", async t => {
  const view = browser();
  t.mock.timers.enable({ apis: ["setInterval"] });
  let reads = 0, writes = 0;
  const queued: McpDiagnostics = { ...failed, failed_servers: failed.failed_servers!.map(server => ({ ...server, retry_pending: true })) };
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => {
    if (init.method === "POST") { writes++; return Response.json({ ...catalog(queued), mcp_retry: { server: "broken", status: "queued" } }); }
    reads++; return Response.json(catalog(reads === 1 ? failed : recovered));
  });
  try {
    await act(async () => view.root.render(createElement(ToolsDialog, { token: "token", blocked: false, close: () => {} })));
    await act(async () => button("Disable visible").click());
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Retry broken"]')!.click());
    assert.match(document.body.textContent!, /Current work continues/);
    assert.equal(document.querySelector<HTMLButtonElement>('[aria-label="Retry broken"]')?.disabled, true);
    await act(async () => t.mock.timers.tick(3000));
    assert.match(document.body.textContent!, /broken connected/);
    assert.doesNotMatch(document.body.textContent!, /retry queued/i);
    assert.equal(document.querySelector<HTMLInputElement>('[role="switch"]')?.checked, false);
    assert.equal(button("Save tools").disabled, false);
    assert.equal(writes, 1);
    assert.equal(reads, 2);
  } finally { await view.cleanup(); t.mock.timers.tick(6000); assert.equal(reads, 2); t.mock.timers.reset(); }
});

test("a retry error retains the failure and draft, reports accessibly, and never retries automatically", async t => {
  const view = browser();
  t.mock.timers.enable({ apis: ["setInterval"] });
  let writes = 0;
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => {
    if (init.method === "POST") { writes++; return Response.json({ detail: "MCP discovery changed. Refresh tools before retrying." }, { status: 409 }); }
    return Response.json(catalog());
  });
  try {
    await act(async () => view.root.render(createElement(ToolsDialog, { token: "token", blocked: false, close: () => {} })));
    await act(async () => button("Disable visible").click());
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Retry broken"]')!.click());
    assert.match(document.querySelector('[role="alert"]')!.textContent!, /MCP discovery changed/);
    assert.equal(document.querySelector<HTMLButtonElement>('[aria-label="Retry broken"]')?.disabled, false);
    await act(async () => t.mock.timers.tick(9000));
    assert.equal(writes, 1);
    assert.equal(document.querySelector<HTMLInputElement>('[role="switch"]')?.checked, false);
    assert.match(document.body.textContent!, /Missing environment variable/);
    assert.equal(button("Save tools").disabled, false);
  } finally { await view.cleanup(); t.mock.timers.reset(); }
});
