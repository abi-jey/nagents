import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { useOperations } from "../app/useOperations.js";
import { useSessions } from "../features/sessions/useSessions.js";

for (const unmount of [false, true]) test(`session connection ${unmount ? "cancels on unmount" : "survives an effect probe and transient busy read"}`, async t => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const root = createRoot(dom.window.document.getElementById("root")!);
  let reads = 0, selected = "", finished = false;
  const failures: unknown[] = [];
  t.mock.method(globalThis, "fetch", async (url: string) => {
    if (url === "/api/bootstrap") return Response.json({ token: "token", active_run_id: "" });
    assert.equal(url, "/api/sessions"); reads++;
    return reads === 1 || unmount ? Response.json({ detail: "Busy" }, { status: 409 })
      : Response.json({ session_id: "ngn-original", sessions: [{ id: "ngn-original", title: "Existing", updated_at: "today" }], history: [], retained_tasks: [] });
  });
  function Probe() {
    const sessions = useSessions(), operations = useOperations();
    selected = sessions.sessionId;
    useEffect(() => {
      // Use the application's actual synchronous operation gate: the effect
      // probe cannot start another connection while the first is admitted.
      void operations.operate(async () => {
        try { await sessions.connect(); }
        catch (cause) { failures.push(cause); throw cause; }
        finally { finished = true; }
      });
    }, []);
    return null;
  }
  try {
    await act(async () => root.render(createElement(StrictMode, null, createElement(Probe))));
    if (unmount) await act(async () => root.unmount());
    const deadline = Date.now() + 5000;
    while (!finished && Date.now() < deadline) await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)); });
    assert.equal(finished, true);
    if (unmount) {
      assert.equal(reads, 1); assert.equal(selected, "");
      assert.equal(failures.length, 1);
      assert.ok(failures[0] instanceof DOMException && failures[0].name === "AbortError");
    } else {
      assert.equal(reads, 2); assert.equal(selected, "ngn-original"); assert.deepEqual(failures, []);
    }
  } finally {
    if (!unmount) await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
