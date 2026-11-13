import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { Dashboard } from "../features/dashboard/Dashboard.js";
import { isDashboardPath, openDashboard, readDashboard } from "../features/dashboard/dashboard.js";
import { commandIntent, commandMatches } from "../features/chat/commands.js";

const bootstrap = { token: "local-dashboard-token", workspace: "/work/project", capabilities: { chat: true } };

function browser() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost/dashboard" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  return { dom, container, root, async cleanup() {
    await act(async () => root.unmount());
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  } };
}

test("dashboard is a local command and opens a noopener tab synchronously", async t => {
  const view = browser();
  const calls: unknown[][] = [];
  t.mock.method(view.dom.window, "open", (...args: unknown[]) => { calls.push(args); return null; });
  try {
    assert.deepEqual(commandIntent("/dashboard", true), { name: "dashboard", argument: "" });
    assert.deepEqual(commandMatches("/dash").map(command => command.name), ["dashboard"]);
    assert.throws(() => commandIntent("/dashboard global"), /Usage: \/dashboard/);
    openDashboard();
    assert.deepEqual(calls, [["/dashboard", "_blank", "noopener,noreferrer"]]);
    assert.equal(isDashboardPath("/dashboard"), true);
    assert.equal(isDashboardPath("/dashboard/"), true);
    assert.equal(isDashboardPath("/dashboard-other"), false);
    assert.equal(isDashboardPath("/"), false);
  } finally { await view.cleanup(); }
});

test("dashboard authenticates metadata and keeps both scopes explicitly unmeasured", async t => {
  const view = browser(), calls: string[] = [];
  t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    calls.push(input);
    assert.equal(init.method, "GET");
    assert.equal(init.mode, "same-origin");
    if (input === "/api/bootstrap") return Response.json(bootstrap);
    assert.equal(input, "/api/dashboard");
    assert.equal((init.headers as Record<string, string>)["X-Ngn-Token"], bootstrap.token);
    return Response.json({ workspace: "/work/project", status: "not_connected" });
  });
  try {
    await act(async () => view.root.render(createElement(Dashboard)));
    assert.equal(view.dom.window.document.title, "Dashboard | ngn");
    assert.equal(view.container.querySelector(".dashboard-back")?.getAttribute("href"), "/");
    assert.match(view.container.textContent!, /Instrumentation not connected/);
    assert.match(view.container.textContent!, /Current server workspace \/work\/project/);
    assert.equal(view.container.querySelectorAll(".dashboard-metric dd").length, 4);
    for (const tile of view.container.querySelectorAll(".dashboard-metric dd")) assert.equal(tile.textContent, "—Unavailable");
    for (const title of ["Skills", "Custom agents", "Models", "Workspaces"]) assert.ok(view.container.textContent!.includes(title));
    await act(async () => view.container.querySelectorAll<HTMLButtonElement>(".dashboard-scope button")[1].click());
    assert.equal(view.container.querySelector('[aria-pressed="true"]')!.textContent, "Global");
    assert.match(view.container.textContent!, /All workspaces \(preview\)/);
    assert.match(view.container.textContent!, /Global data will appear here when connected/);
    assert.deepEqual(calls, ["/api/bootstrap", "/api/dashboard"], "Scope previews must not invent analytics or load resource inventories");
    assert.doesNotMatch(view.container.textContent!, /local-dashboard-token/);
  } finally { await view.cleanup(); }
});

test("metadata failure offers retry without leaking server details or hiding the skeleton", async t => {
  const view = browser(); let attempts = 0;
  t.mock.method(globalThis, "fetch", async (input: string) => {
    if (input === "/api/bootstrap") return Response.json(bootstrap);
    if (++attempts === 1) return Response.json({ detail: "private credential detail" }, { status: 500 });
    return Response.json({ workspace: "/retry/project", status: "not_connected" });
  });
  try {
    await act(async () => view.root.render(createElement(Dashboard)));
    assert.match(view.container.textContent!, /Could not load local workspace metadata/);
    assert.match(view.container.textContent!, /Instrumentation not connected/);
    assert.doesNotMatch(view.container.textContent!, /private credential detail/);
    await act(async () => view.container.querySelector<HTMLButtonElement>(".dashboard-connection button")!.click());
    assert.match(view.container.textContent!, /\/retry\/project/);
    assert.doesNotMatch(view.container.textContent!, /Could not load/);
  } finally { await view.cleanup(); }
});

test("dashboard cancels requests on unmount and ignores late metadata", async t => {
  const view = browser();
  let signal: AbortSignal | undefined;
  let resolve!: (value: Response) => void;
  t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    if (input === "/api/bootstrap") return Response.json(bootstrap);
    signal = init.signal as AbortSignal;
    return new Promise<Response>(done => { resolve = done; });
  });
  try {
    await act(async () => view.root.render(createElement(Dashboard)));
    assert.match(view.container.textContent!, /Loading local workspace metadata/);
    await act(async () => view.root.render(null));
    assert.equal(signal?.aborted, true);
    await act(async () => resolve(Response.json({ workspace: "/late", status: "not_connected" })));
    assert.equal(view.container.childElementCount, 0);
  } finally { await view.cleanup(); }
});

test("dashboard metadata validates the disconnected contract", async t => {
  t.mock.method(globalThis, "fetch", async (input: string) => Response.json(input === "/api/bootstrap" ? bootstrap : { workspace: 12, status: "connected" }));
  await assert.rejects(readDashboard(new AbortController().signal), /Dashboard metadata is unavailable/);
});

for (const capabilities of [{ chat: false }, undefined]) {
  test(`standalone dashboard omits chat navigation (${JSON.stringify(capabilities)})`, async t => {
    const view = browser();
    t.mock.method(globalThis, "fetch", async (input: string) => Response.json(input === "/api/bootstrap"
      ? { ...bootstrap, capabilities } : { workspace: "/work/project", status: "not_connected" }));
    try {
      await act(async () => view.root.render(createElement(Dashboard)));
      assert.equal(view.container.querySelector(".dashboard-back"), null);
      assert.equal(view.container.querySelector(".dashboard-brand")?.getAttribute("href"), "/dashboard");
      assert.equal(view.container.querySelector(".dashboard-brand")?.getAttribute("aria-label"), "ngn dashboard");
    } finally { await view.cleanup(); }
  });
}
