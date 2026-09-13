import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { deleteSession, SessionDeletion, type DeletionState } from "./deletion.js";
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
    if (++count === 1) throw new Error("Session has queued inbox work.");
    return true;
  });
  controller.show(target); await controller.confirm();
  assert.equal(count, 1); assert.equal(controller.state.pending, false);
  assert.equal(controller.state.target?.id, target.id);
  assert.match(controller.state.error, /queued inbox/);
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
    assert.equal(options.body, "{}"); assert.equal(options.credentials, "same-origin");
    assert.equal(options.cache, "no-store");
    assert.deepEqual(options.headers, { "X-Ngn-Token": "synthetic", "Content-Type": "application/json" });
    return Response.json(snapshot);
  });
  assert.deepEqual(await deleteSession("synthetic", target.id), snapshot);
});

test("unconfirmed or inconsistent deletion acknowledgement cannot clear local history", async (context) => {
  const replies = [ {}, { ...snapshot, deleted_session_id: "ngn-wrong" }, { ...snapshot, session_id: target.id },
    { ...snapshot, sessions: [target] }, { ...snapshot, sessions: [...snapshot.sessions, target] } ];
  for (const reply of replies) {
    const mocked = context.mock.method(globalThis, "fetch", async () => Response.json(reply));
    await assert.rejects(deleteSession("synthetic", target.id), /acknowledgement was not confirmed/);
    mocked.mock.restore();
  }
});

test("HTTP deletion conflicts preserve actionable routing instructions", async (context) => {
  context.mock.method(globalThis, "fetch", async () => Response.json({ detail: "Reattach with /session ID first." }, { status: 409 }));
  await assert.rejects(deleteSession("synthetic", target.id), /Reattach with \/session ID/);
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
    controller, state: { target: { ...target, title: "<script>private title</script>" }, pending: true, error: "Reattach channel first" },
  }));
  assert.match(html, /<dialog[^>]*aria-labelledby="delete-session-title"[^>]*aria-describedby="delete-session-description"[^>]*aria-busy="true"/);
  assert.match(html, /cannot be undone/); assert.match(html, /&lt;script&gt;private title&lt;\/script&gt;/);
  assert.match(html, /role="alert"[^>]*>Reattach channel first/);
  assert.match(html, /role="status">Deleting session/);
  assert.match(html, /<button disabled="">Cancel<\/button>/);
  assert.match(html, /disabled="">Delete session<\/button>/);
});
