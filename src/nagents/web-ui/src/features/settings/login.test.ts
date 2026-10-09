import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { LoginDialog } from "./LoginDialog.js";
import { deviceLoginUrl, parseLogin, type LoginReply } from "./login.js";

const idle: LoginReply = { id: "", status: "idle", verification_url: "", user_code: "", expires_in: 0, message: "Start sign-in", provider: "openai" };
const pending: LoginReply = { ...idle, id: "attempt-1", status: "pending", verification_url: deviceLoginUrl, user_code: "ABCD-EFGH", expires_in: 120, message: "Waiting for you" };

test("device login parser pins the verification origin and clears terminal codes", () => {
  assert.deepEqual(parseLogin(pending), pending);
  assert.throws(() => parseLogin({ ...pending, verification_url: "https://untrusted.example" }), /Invalid login/);
  assert.throws(() => parseLogin({ ...pending, expires_in: NaN }), /Invalid login/);
  assert.equal(parseLogin({ ...pending, status: "completed" }).user_code, "");
  assert.equal(parseLogin({ ...pending, status: "cancelled" }).verification_url, "");
});

for (const outcome of ["completed", "cancelled", "lost-response"] as const) test(`device login requests stay on ngn and ${outcome} clears the private code`, async t => {
  const dom = new JSDOM("<button id='opener'>Login</button><div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
  Object.defineProperties(dom.window.HTMLDialogElement.prototype, { showModal: { value() {} }, close: { value() {} } });
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  let status = idle, starts = 0, closes = 0, applied = 0, poll = () => {};
  t.mock.method(globalThis, "setInterval", ((callback: () => void) => { poll = callback; return 1; }) as typeof setInterval);
  t.mock.method(globalThis, "clearInterval", () => {});
  const requests: string[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    requests.push(`${init.method} ${path}`);
    assert.equal(init.mode, "same-origin"); assert.equal(init.credentials, "same-origin");
    assert.equal(new Headers(init.headers).get("X-Ngn-Token"), "token");
    if (init.method === "POST" && path === "/api/login/chatgpt") {
      starts++; status = pending;
      if (outcome === "lost-response") throw new Error("Connection lost after start was accepted");
    }
    if (path === "/api/login/chatgpt/cancel") {
      assert.deepEqual(JSON.parse(String(init.body)), { id: pending.id });
      status = { ...pending, status: "cancelled", user_code: "", verification_url: "" };
    }
    return Response.json(status);
  });
  const button = (label: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === label)!;
  try {
    await act(async () => root.render(createElement(LoginDialog, { token: "token", close: () => { closes++; }, applied: async () => { applied++; } })));
    assert.equal(starts, 0, "Opening /login does not silently start OAuth");
    await act(async () => button("Sign in with ChatGPT").click());
    assert.equal(starts, 1);
    assert.equal(container.querySelector("output")!.textContent, pending.user_code);
    assert.equal(container.querySelector("a")!.href, deviceLoginUrl);
    assert.equal(container.querySelector("a")!.rel, "noopener noreferrer");
    if (outcome === "completed") {
      status = { ...pending, status: "completed", message: "Signed in" };
      await act(async () => poll());
      assert.equal(applied, 1);
      await act(async () => poll());
      assert.equal(applied, 1, "A terminal flow refreshes the connection only once");
      assert.equal(closes, 0);
      assert.ok(button("Sign in again"), "Completed attempts can be replaced by a fresh explicit sign-in");
    } else {
      await act(async () => button("Cancel sign-in").click());
      assert.equal(closes, 1);
      assert.equal(applied, 0);
    }
    assert.equal(container.querySelector("output"), null);
    assert.ok(requests.every(request => request.includes(" /api/login/chatgpt")));
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    }
  }
});
