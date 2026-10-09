import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { changeSession, sessionTitle } from "./sessionActions.js";
import { validSnapshot } from "./subscription.js";
import { useSessions } from "../features/sessions/useSessions.js";
import { SessionMenu } from "../features/sessions/SessionMenu.js";
import type { Snapshot } from "../types.js";

const source = { id: "source", title: "Original", updated_at: "today" };
const other = { id: "other", title: "Other", updated_at: "today" };
const initial: Snapshot = { session_id: source.id, sessions: [source, other], history: [], retained_tasks: [],
  active_runs: [{ id: "run-other", session_id: other.id, status: "running" }] };

test("chat titles preserve unicode while requiring bounded printable rename values", () => {
  assert.equal(sessionTitle("  New title  ", true), "New title");
  assert.equal(sessionTitle("😀".repeat(80), true), "😀".repeat(80));
  assert.equal(sessionTitle("", false), "");
  for (const value of [" ", "x".repeat(81), "name\nnew", "name\u0000", "a\u200db", "a\u2028b", "name\ud800"])
    assert.throws(() => sessionTitle(value, true), /chat title/);
  assert.equal(validSnapshot({ ...initial, sessions: [{ ...source, forked_from: 5 }] }), false);
});

test("fork and rename use explicit local actions and never replay a failed fork", async t => {
  const calls: { path: string; body: unknown }[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    calls.push({ path, body: JSON.parse(String(init.body)) });
    return path.endsWith("/fork") ? Response.json({ detail: "Source chat has queued work." }, { status: 409 }) : Response.json(initial);
  });
  await changeSession("token", source.id, "rename", "Named chat");
  await assert.rejects(changeSession("token", source.id, "fork", ""), /queued work/);
  await assert.rejects(changeSession("token", "../source", "fork", ""), /Invalid chat/);
  assert.deepEqual(calls, [
    { path: "/api/sessions/source/rename", body: { title: "Named chat" } },
    { path: "/api/sessions/source/fork", body: { title: "" } },
  ]);
});

test("renaming another chat preserves selection and active owners; fork accepts its own copied snapshot", async t => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const root = createRoot(dom.window.document.getElementById("root")!);
  let state!: ReturnType<typeof useSessions>;
  const copied: Snapshot = { ...initial, session_id: "forked", sessions: [
    { id: "forked", title: "Branch", updated_at: "today", forked_from: source.id }, source, { ...other, title: "Renamed" },
  ], history: [{ role: "user", content: "Copied source context", name: "", tool_call_id: "", tool_calls: [] }] };
  t.mock.method(globalThis, "fetch", async (path: string) => {
    if (path === "/api/bootstrap") return Response.json({ token: "token", active_run_id: "", active_runs: initial.active_runs });
    if (path === "/api/sessions") return Response.json(initial);
    if (path === "/api/sessions/other/rename") return Response.json({ ...initial, sessions: [source, { ...other, title: "Renamed" }] });
    if (path === "/api/sessions/source/fork") return Response.json(copied);
    throw new Error(`Unexpected request: ${path}`);
  });
  function Probe() { state = useSessions(); return null; }
  try {
    await act(async () => root.render(createElement(Probe)));
    await act(async () => { await state.connect(); });
    await act(async () => { await state.rename(other.id, "Renamed"); });
    assert.equal(state.sessionId, source.id);
    assert.equal(state.sessions.find(item => item.id === other.id)!.title, "Renamed");
    assert.equal(state.globalRunId, "run-other");
    let result: Snapshot | undefined;
    await act(async () => { result = await state.fork(source.id, "Branch"); });
    assert.deepEqual(result, copied);
    assert.equal(state.sessionId, "forked");
    assert.equal(state.sessions[0].forked_from, source.id);
    assert.equal(state.globalRunId, "run-other");
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"])
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
  }
});

test("active chat menus keep rename and move available while rejecting fork and deletion", () => {
  const dom = new JSDOM(renderToStaticMarkup(createElement(SessionMenu, {
    session: { ...source, active_run_id: "run" }, disabled: true, permanent() {}, move() {}, rename() {}, fork() {},
  })));
  const items = [...dom.window.document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
  assert.equal(items.find(item => item.textContent!.includes("Rename"))!.disabled, false);
  assert.equal(items.find(item => item.textContent!.includes("Move"))!.disabled, false);
  assert.equal(items.find(item => item.textContent!.includes("Fork"))!.disabled, true);
  assert.equal(items.find(item => item.textContent!.includes("Delete"))!.disabled, true);
  dom.window.close();
});
