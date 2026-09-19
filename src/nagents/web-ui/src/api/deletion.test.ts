import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { deleteSession, deleteSessionFromView, restoreDeletionFocus, SessionDeletion, type DeletionState, type DeletionView } from "./deletion.js";
import { DeleteSessionDialog } from "../features/sessions/DeleteSessionDialog.js";
import { LiveSessions } from "../features/chat/liveTranscript.js";
import { MessageQueue } from "./messages.js";
import { subscribeEvents, type EventSocket } from "./subscription.js";
import { deferred } from "../features/dictation/testFixtures.js";
import type { Snapshot } from "../types.js";

const target = { id: "ngn-selected", title: "Selected conversation", updated_at: "2026-01-01" };
const snapshot: Snapshot & { deleted_session_id: string } = {
  deleted_session_id: target.id, session_id: "ngn-survivor", history: [], retained_tasks: [],
  sessions: [{ id: "ngn-survivor", title: "New session", updated_at: "2026-01-01" }],
};

test("opening and cancelling confirmation never submits or discards a draft", async () => {
  let calls = 0;
  const states: DeletionState[] = [];
  const controller = new SessionDeletion((state) => states.push(state), async () => { calls++; return true; });
  await controller.confirm();
  controller.show(target);
  assert.equal(controller.state.target?.id, target.id);
  controller.cancel(); await controller.confirm();
  assert.equal(calls, 0);
  assert.deepEqual(states.at(-1), { pending: false, error: "" });
});

test("confirmation owns one captured session until success; pending blocks cancel and duplicate clicks", async () => {
  const pending = deferred<boolean>();
  const ids: string[] = [];
  let draft = "unsent text";
  const controller = new SessionDeletion(() => {}, async (id) => {
    ids.push(id);
    const result = await pending.promise;
    if (result) draft = "";
    return result;
  });
  controller.show(target);
  const request = controller.confirm();
  controller.cancel(); controller.show({ ...target, id: "ngn-other" }); await controller.confirm();
  assert.deepEqual(ids, [target.id]); assert.equal(controller.state.pending, true);
  assert.equal(draft, "unsent text");
  pending.resolve(true); await request;
  assert.equal(draft, ""); assert.equal(controller.state.target, undefined);
});

test("server failure leaves the named confirmation and draft available for cancellation or explicit retry", async () => {
  let count = 0;
  const controller = new SessionDeletion(() => {}, async () => {
    if (++count === 1) throw new Error("Session has retained descendant tasks.");
    return true;
  });
  controller.show(target); await controller.confirm();
  assert.equal(count, 1); assert.equal(controller.state.pending, false);
  assert.equal(controller.state.target?.id, target.id);
  assert.match(controller.state.error, /retained descendant tasks/);
  await controller.confirm(); assert.equal(count, 2); assert.equal(controller.state.target, undefined);
});

test("client operation conflicts are visible instead of closing confirmation", async () => {
  const controller = new SessionDeletion(() => {}, async () => false);
  controller.show(target); await controller.confirm();
  assert.equal(controller.state.target?.id, target.id);
  assert.match(controller.state.error, /Finish the current operation/);
});

test("DELETE sends authenticated same-origin JSON and validates a surviving selected snapshot", async (context) => {
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    assert.equal(url, `/api/sessions/${target.id}`); assert.equal(options.method, "DELETE");
    assert.equal(options.body, '{"permanent":true}'); assert.equal(options.credentials, "same-origin");
    assert.equal(options.cache, "no-store");
    assert.deepEqual(options.headers, { "X-Ngn-Token": "synthetic", "Content-Type": "application/json" });
    return Response.json(snapshot);
  });
  assert.deepEqual(await deleteSession("synthetic", target.id, true), snapshot);
});

test("unconfirmed or inconsistent deletion acknowledgement cannot clear local history", async (context) => {
  const replies = [ {}, { ...snapshot, deleted_session_id: "ngn-wrong" }, { ...snapshot, session_id: target.id },
    { ...snapshot, sessions: [target] }, { ...snapshot, sessions: [...snapshot.sessions, target] } ];
  for (const reply of replies) {
    const mocked = context.mock.method(globalThis, "fetch", async () => Response.json(reply));
    await assert.rejects(deleteSession("synthetic", target.id, true), /acknowledgement was not confirmed/);
    mocked.mock.restore();
  }
});

test("HTTP deletion conflicts preserve actionable retry instructions", async (context) => {
  const detail = "Session has retained descendant tasks. Finish or cancel them, then restart ngn before deleting this root.";
  context.mock.method(globalThis, "fetch", async () => Response.json({ detail }, { status: 409 }));
  await assert.rejects(deleteSession("synthetic", target.id, true), /retained descendant tasks/);
});

test("forgetting a deleted root drops its transcript, replay cursor and uncertain message identities only", () => {
  const cache = new LiveSessions(); const queue = new MessageQueue();
  cache.set(target.id, { entries: [], position: { cursor: 7, epoch: "old" } });
  cache.set("ngn-other", { entries: [], position: { cursor: 9, epoch: "old" } });
  const original = queue.prepare(target.id, "draft", () => "old");
  const other = queue.prepare("ngn-other", "draft", () => "other");
  cache.forget(target.id); queue.forget(target.id);
  assert.equal(cache.get(target.id).position, undefined);
  assert.equal(cache.get("ngn-other").position?.cursor, 9);
  assert.notEqual(queue.prepare(target.id, "draft", () => "fresh"), original);
  assert.equal(queue.prepare("ngn-other", "draft"), other);
});

test("deleted-root policy close stops retries and asks the owner to refresh membership once", () => {
  let unavailable = 0;
  const timers: { cancelled: boolean }[] = [];
  const socket: EventSocket = { send() {}, close() {}, onopen: null, onmessage: null, onerror: null, onclose: null };
  const stop = subscribeEvents({ token: "synthetic", sessionId: target.id, url: "http://localhost:8765",
    signal: new AbortController().signal, receive() {}, status() {}, socket: () => socket,
    unavailable: () => { unavailable++; }, schedule: () => { const timer = { cancelled: false }; timers.push(timer); return () => { timer.cancelled = true; }; },
  });
  const close = socket.onclose;
  close?.call(socket as WebSocket, { code: 1008 } as CloseEvent);
  close?.call(socket as WebSocket, { code: 1008 } as CloseEvent);
  assert.equal(unavailable, 1); assert.ok(timers.every((timer) => timer.cancelled));
  assert.equal(socket.onmessage, null); stop();
});

test("confirmation uses a named native dialog, explicit irreversible text, cancel and live error/pending states", () => {
  const controller = new SessionDeletion(() => {}, async () => assert.fail("render cannot delete"));
  const html = renderToStaticMarkup(createElement(DeleteSessionDialog, {
    controller, state: { target: { ...target, title: "<script>private title</script>" }, pending: true, error: "Finish retained descendant tasks first" },
  }));
  assert.match(html, /<dialog[^>]*aria-labelledby="delete-session-title"[^>]*aria-describedby="delete-session-description"[^>]*aria-busy="true"/);
  assert.match(html, /cannot be undone/); assert.match(html, /&lt;script&gt;private title&lt;\/script&gt;/);
  assert.match(html, /role="alert"[^>]*>Finish retained descendant tasks first/);
  assert.match(html, /role="status">Deleting session/);
  assert.match(html, /<button disabled="">Cancel<\/button>/);
  assert.match(html, /disabled="">Delete forever<\/button>/);
});

function deletionView() {
  const pending = deferred<Snapshot>();
  const state = {
    selection: { id: target.id, revision: 1 },
    sessions: [target.id, "ngn-sidebar"],
    draft: "Keep my unsent draft",
    history: ["Current conversation"],
    review: "Edited dictation review",
    subscription: "original subscription",
    requests: [] as string[],
    forgotten: [] as { id: string; discardDraft: boolean }[],
    pauses: 0,
    reconnects: 0,
  };
  const view: DeletionView = {
    selection: () => ({ ...state.selection }),
    remove: (id) => { state.requests.push(id); return pending.promise; },
    drop: (id) => { state.sessions = state.sessions.filter((session) => session !== id); },
    forget: (id, discardDraft) => { state.forgotten.push({ id, discardDraft }); if (discardDraft) state.draft = ""; },
    replace: (next) => {
      state.selection = { id: next.session_id, revision: state.selection.revision + 1 };
      state.sessions = next.sessions.map((session) => session.id);
      state.history = next.history.map((message) => message.content);
      state.review = "";
    },
    pause: () => { state.pauses++; state.subscription = "paused"; },
    reconnect: () => { state.reconnects++; state.subscription = "replacement subscription"; },
  };
  return { state, pending, view };
}

test("deleting another sidebar root ignores a different server selection and preserves the entire current view", async (context) => {
  const f = deletionView();
  const response = deferred<Response>();
  const requests: string[] = [];
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    requests.push(`${options.method} ${url}`);
    return response.promise;
  });
  f.view.remove = (id) => deleteSession("synthetic", id, true);
  const removing = deleteSessionFromView("ngn-sidebar", f.view, true);
  assert.equal(f.state.draft, "Keep my unsent draft");
  assert.equal(f.state.review, "Edited dictation review");
  assert.equal(f.state.subscription, "original subscription");
  response.resolve(Response.json({
    ...snapshot, deleted_session_id: "ngn-sidebar", session_id: "ngn-third-tab",
    sessions: [target, { ...target, id: "ngn-third-tab" }],
    history: [{ role: "user", content: "Different tab history", name: "", tool_call_id: "", tool_calls: [] }],
  }));
  await removing;
  assert.deepEqual(requests, ["DELETE /api/sessions/ngn-sidebar"], "No resume or other selection mutation is sent");
  assert.deepEqual(f.state.selection, { id: target.id, revision: 1 });
  assert.deepEqual(f.state.history, ["Current conversation"]);
  assert.equal(f.state.draft, "Keep my unsent draft");
  assert.equal(f.state.review, "Edited dictation review");
  assert.equal(f.state.subscription, "original subscription");
  assert.equal(f.state.pauses, 0); assert.equal(f.state.reconnects, 0);
  assert.deepEqual(f.state.sessions, [target.id]);
  assert.deepEqual(f.state.forgotten, [{ id: "ngn-sidebar", discardDraft: false }]);
});

test("confirmed current-root deletion clears its draft and loads the surviving root exactly once", async () => {
  const f = deletionView();
  const removing = deleteSessionFromView(target.id, f.view, true);
  assert.equal(f.state.draft, "Keep my unsent draft");
  assert.deepEqual(f.state.history, ["Current conversation"]);
  assert.equal(f.state.pauses, 1); assert.equal(f.state.reconnects, 0);
  f.pending.resolve(snapshot); await removing;
  assert.deepEqual(f.state.requests, [target.id]);
  assert.equal(f.state.selection.id, snapshot.session_id);
  assert.deepEqual(f.state.history, []);
  assert.equal(f.state.draft, "");
  assert.deepEqual(f.state.forgotten, [{ id: target.id, discardDraft: true }]);
  assert.equal(f.state.reconnects, 1);
});

for (const id of [target.id, "ngn-sidebar"]) {
  test(`failed deletion of ${id} keeps selection, history, draft, review and session membership`, async () => {
    const f = deletionView();
    const removing = deleteSessionFromView(id, f.view);
    f.pending.reject(new Error("Finish retained descendant tasks first"));
    await assert.rejects(removing, /retained descendant tasks/);
    assert.deepEqual(f.state.selection, { id: target.id, revision: 1 });
    assert.deepEqual(f.state.history, ["Current conversation"]);
    assert.equal(f.state.draft, "Keep my unsent draft");
    assert.equal(f.state.review, "Edited dictation review");
    assert.deepEqual(f.state.sessions, [target.id, "ngn-sidebar"]);
    assert.deepEqual(f.state.forgotten, []);
    assert.equal(f.state.pauses, id === target.id ? 1 : 0);
    assert.equal(f.state.reconnects, id === target.id ? 1 : 0);
  });
}

for (const id of [target.id, "ngn-sidebar"]) {
  for (const selectedAgain of [false, true]) {
    test(`late acknowledgement for ${id} cannot reset a newer ${selectedAgain ? "away-and-back" : "different"} selection`, async () => {
      const f = deletionView();
      const removing = deleteSessionFromView(id, f.view);
      f.state.selection = { id: selectedAgain ? target.id : "ngn-newer", revision: 3 };
      f.state.sessions.push("ngn-newer");
      f.state.draft = "Newer draft"; f.state.history = ["Newer history"];
      f.state.review = "Newer dictation review"; f.state.subscription = "newer subscription";
      f.pending.resolve(snapshot); await removing;
      assert.deepEqual(f.state.selection, { id: selectedAgain ? target.id : "ngn-newer", revision: 3 });
      assert.deepEqual(f.state.history, ["Newer history"]);
      assert.equal(f.state.draft, "Newer draft"); assert.equal(f.state.review, "Newer dictation review");
      assert.equal(f.state.subscription, "newer subscription"); assert.equal(f.state.reconnects, 0);
      assert.ok(f.state.sessions.includes("ngn-newer"));
      assert.deepEqual(f.state.forgotten, [{ id, discardDraft: false }]);
    });
  }
}

test("confirmation copy distinguishes current from noncurrent deletion, including captured sidebar identity", () => {
  const controller = new SessionDeletion(() => {}, async () => true, () => target.id);
  controller.show({ ...target, id: "ngn-sidebar" });
  const other = renderToStaticMarkup(createElement(DeleteSessionDialog, { controller, state: controller.state }));
  assert.match(other, /Your current conversation and draft are kept/);
  assert.doesNotMatch(other, /draft will be discarded/);
  const current = renderToStaticMarkup(createElement(DeleteSessionDialog, {
    controller, state: controller.state, currentSessionId: "ngn-sidebar",
  }));
  assert.match(current, /draft will be discarded only after deletion is confirmed/);
  assert.doesNotMatch(current, /current conversation and draft are kept/);
});

type FocusCondition = "available" | "removed" | "disabled" | "hidden" | "inert-ancestor" | "hidden-ancestor" | "css-hidden" | "transparent" | "blocked";
function focusFixture() {
  const focused: string[] = [];
  const ownerDocument: { activeElement: HTMLElement | null; defaultView: object } = {
    activeElement: null,
    defaultView: { getComputedStyle: (element: HTMLElement & { fixtureStyle: object }) => element.fixtureStyle },
  };
  function element(name: string, condition: FocusCondition = "available"): HTMLElement {
    const target = {
      ownerDocument,
      isConnected: condition !== "removed",
      matches: () => condition === "disabled",
      closest: () => condition === "inert-ancestor" || condition === "hidden-ancestor" ? {} : null,
      getClientRects: () => ({ length: condition === "hidden" ? 0 : 1 }),
      fixtureStyle: { visibility: condition === "css-hidden" ? "hidden" : "visible", opacity: condition === "transparent" ? "0" : "1" },
      focus: (options: FocusOptions) => {
        assert.equal(options.preventScroll, true);
        focused.push(name);
        if (condition !== "blocked") ownerDocument.activeElement = target as unknown as HTMLElement;
      },
    };
    return target as unknown as HTMLElement;
  }
  function navigation(selected: HTMLElement | null, close: HTMLElement | null): HTMLElement {
    return { querySelector: (selector: string) => {
      if (selector === ".session-row[data-session-id] > .session[aria-current='page']:not(:disabled)") return selected;
      assert.equal(selector, ".sidebar-close");
      return close;
    } } as unknown as HTMLElement;
  }
  return { focused, ownerDocument, element, navigation };
}

test("desktop focus returns to a surviving trigger or composer, skipping hidden, inert and disabled triggers", () => {
  for (const condition of ["available", "removed", "disabled", "hidden", "inert-ancestor", "hidden-ancestor", "css-hidden", "transparent"] as const) {
    const f = focusFixture();
    restoreDeletionFocus(f.element("trigger", condition), f.element("composer"));
    assert.deepEqual(f.focused, [condition === "available" ? "trigger" : "composer"]);
  }
});

test("open mobile drawer restores a surviving trigger before its selected session", () => {
  const f = focusFixture();
  restoreDeletionFocus(f.element("trigger"), f.element("composer", "inert-ancestor"),
    f.navigation(f.element("selected"), f.element("close")));
  assert.deepEqual(f.focused, ["trigger"]);
});

test("deleting a mobile sidebar row returns focus to the surviving selected session, never the inert composer", () => {
  const f = focusFixture();
  restoreDeletionFocus(f.element("trigger", "removed"), f.element("composer", "inert-ancestor"),
    f.navigation(f.element("selected"), f.element("close")));
  assert.deepEqual(f.focused, ["selected"]);
});

test("mobile focus falls back to drawer close when the selected session is absent, disabled, hidden or inert", () => {
  for (const condition of ["removed", "disabled", "hidden", "inert-ancestor", "css-hidden"] as const) {
    const f = focusFixture();
    restoreDeletionFocus(null, f.element("composer", "inert-ancestor"),
      f.navigation(condition === "removed" ? null : f.element("selected", condition), f.element("close")));
    assert.deepEqual(f.focused, ["close"]);
  }
});

test("focus restoration skips an unavailable fallback and continues after an implicitly blocked focus attempt", () => {
  const f = focusFixture();
  restoreDeletionFocus(null, f.element("composer", "inert-ancestor"),
    f.navigation(f.element("selected", "blocked"), f.element("close")));
  assert.deepEqual(f.focused, ["selected", "close"]);
  f.focused.length = 0;
  restoreDeletionFocus(null, f.element("composer", "inert-ancestor"),
    f.navigation(null, f.element("close", "hidden")));
  assert.deepEqual(f.focused, []);
});

test("a removed Trash row returns confirmation focus to the underlying native Trash dialog, not its inert drawer", () => {
  const f = focusFixture();
  const trashClose = f.element("trash-close");
  restoreDeletionFocus(f.element("purged-row", "removed"), trashClose,
    f.navigation(f.element("drawer-selected", "blocked"), f.element("drawer-close", "blocked")));
  assert.equal(f.ownerDocument.activeElement, trashClose);
  assert.deepEqual(f.focused, ["drawer-selected", "drawer-close", "trash-close"]);
});
