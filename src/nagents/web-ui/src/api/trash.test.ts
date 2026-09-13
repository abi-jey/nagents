import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { RequestError } from "./client.js";
import { deleteSession, deleteSessionFromView, SessionDeletion, type DeletionView } from "./deletion.js";
import { purgeTrash, readTrash, restoreTrash, retentionDays, saveRetention, validTrashItem, type TrashItem, type TrashSnapshot } from "./trash.js";
import { TrashController, type TrashDependencies } from "../features/sessions/trashController.js";
import { TrashDialog } from "../features/sessions/TrashDialog.js";
import { DeleteSessionDialog } from "../features/sessions/DeleteSessionDialog.js";
import { SessionSidebar } from "../features/sessions/SessionSidebar.js";
import { handleMenuKey, menuKey, menuPosition, SessionMenu } from "../features/sessions/SessionMenu.js";
import { deferred } from "../features/dictation/testFixtures.js";
import type { Snapshot } from "../types.js";

const session = { id: "ngn-soft", title: "Soft deleted conversation", updated_at: "2030-01-01" };
const item: TrashItem = { id: session.id, title: session.title, deleted_at: 2000000000, purge_at: 2002592000, deletion_id: "opaque-generation-a" };
const trash: TrashSnapshot = { revision: "opaque-policy-a", retention_days: 30, items: [item] };
const snapshot: Snapshot = { session_id: "ngn-other", history: [], sessions: [{ ...session, id: "ngn-other" }], retained_tasks: [] };

test("soft delete sends a single explicit permanent:false and requires a matching valid tombstone before changing local state", async (context) => {
  const calls: string[] = [];
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    calls.push(url); assert.equal(options.method, "DELETE"); assert.equal(options.body, '{"permanent":false}');
    return Response.json({ ...snapshot, deleted_session_id: item.id, trash: item });
  });
  assert.deepEqual((await deleteSession("synthetic", item.id)).trash, item);
  assert.deepEqual(calls, [`/api/sessions/${item.id}`]);
});

test("missing/mismatched/malformed soft-delete tombstones never confirm a destructive local transition", async (context) => {
  for (const invalid of [undefined, { ...item, id: "ngn-wrong" }, { ...item, deletion_id: "" }, { ...item, purge_at: item.deleted_at }]) {
    const mocked = context.mock.method(globalThis, "fetch", async () => Response.json({ ...snapshot, deleted_session_id: item.id, trash: invalid }));
    await assert.rejects(deleteSession("synthetic", item.id), /acknowledgement was not confirmed/);
    assert.equal(mocked.mock.callCount(), 1, "An invalid acknowledgement must not retry deletion");
    mocked.mock.restore();
  }
});

for (const outcome of ["old-server-422", "missing-trash", "server-error", "network-error"]) {
  test(`soft deletion ${outcome} never falls back to an empty body, retries, or reports success`, async (context) => {
    let calls = 0;
    context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
      calls++;
      assert.equal(url, `/api/sessions/${item.id}`);
      assert.equal(options.method, "DELETE");
      assert.deepEqual(JSON.parse(String(options.body)), { permanent: false });
      if (outcome === "network-error") throw new TypeError("Connection lost");
      if (outcome === "missing-trash") return Response.json({ ...snapshot, deleted_session_id: item.id });
      return Response.json({ detail: "Request rejected" }, { status: outcome === "old-server-422" ? 422 : 500 });
    });
    const f = fixture();
    const viewEffects: string[] = [];
    f.deps.softDelete = async (id) => {
      const reply = await deleteSessionFromView(id, {
        selection: () => ({ id: item.id, revision: 1 }),
        remove: (root) => deleteSession("synthetic", root),
        drop: () => assert.fail("Failure cannot remove membership"),
        forget: () => assert.fail("Failure cannot discard draft/history"),
        replace: () => assert.fail("Failure cannot change the selected conversation"),
        pause: () => viewEffects.push("pause"), reconnect: () => viewEffects.push("reconnect"),
      });
      assert.ok(reply.trash);
      return reply.trash;
    };
    await assert.rejects(f.controller.move(session));
    assert.equal(calls, 1);
    assert.equal(f.controller.getSnapshot().notice, undefined, "No success or Undo notice without a valid tombstone");
    assert.equal(f.controller.getSnapshot().pending, "");
    assert.ok(f.controller.getSnapshot().error);
    assert.deepEqual(viewEffects, ["pause", "reconnect"]);
    f.controller.dispose();
  });
}

for (const current of [true, false]) test(`soft deletion preserves the draft with current=${current}, only replacing deleted current history`, async () => {
  let draft = "User draft"; let selected = { id: current ? item.id : snapshot.session_id, revision: 1 };
  const events: string[] = []; const pending = deferred<Snapshot>();
  const view: DeletionView = {
    selection: () => ({ ...selected }), remove: () => pending.promise, drop: () => events.push("drop"),
    forget: (_, discard) => { events.push("forget"); if (discard) draft = ""; },
    replace: (reply) => { events.push("replace"); selected = { id: reply.session_id, revision: 2 }; },
    pause: () => events.push("pause"), reconnect: () => events.push("reconnect"),
  };
  const deleting = deleteSessionFromView(item.id, view);
  assert.equal(draft, "User draft"); pending.resolve(snapshot); await deleting;
  assert.equal(draft, "User draft"); assert.equal(selected.id, snapshot.session_id);
  assert.deepEqual(events, current ? ["pause", "drop", "forget", "replace", "reconnect"] : ["drop", "forget"]);
});

test("Trash HTTP reads and policy updates use exact authentication, revision, bounds and methods", async (context) => {
  const controller = new AbortController(); const methods: string[] = [];
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    methods.push(`${options.method} ${url}`);
    assert.equal((options.headers as Record<string, string>)["X-Ngn-Token"], "synthetic");
    assert.equal(options.cache, "no-store"); assert.equal(options.credentials, "same-origin");
    if (options.method === "GET") assert.equal(options.signal, controller.signal);
    else { assert.equal(options.signal, undefined); assert.deepEqual(JSON.parse(String(options.body)), { revision: trash.revision, retention_days: 365 }); }
    return Response.json(trash);
  });
  assert.deepEqual(await readTrash("synthetic", controller.signal), trash);
  assert.deepEqual(await saveRetention("synthetic", trash.revision, "365"), trash);
  assert.deepEqual(methods, ["GET /api/trash", "PUT /api/trash/settings"]);
});

test("retention accepts integer days 1–365 and rejects coercions before sending", async (context) => {
  context.mock.method(globalThis, "fetch", async () => assert.fail("Invalid settings must not reach fetch"));
  assert.equal(retentionDays("1"), 1); assert.equal(retentionDays("365"), 365);
  for (const invalid of ["", "0", "366", "1.5", "1e2", "-1", "NaN", "Infinity", "3 days"])
    await assert.rejects(saveRetention("synthetic", trash.revision, invalid), /1 to 365/);
});

test("Trash response rejects invalid timestamps, generations, duplicate identities and noninteger policy", async (context) => {
  for (const value of [{ ...trash, retention_days: 1.5 }, { ...trash, retention_days: "30" },
    { ...trash, revision: "" }, { ...trash, items: [item, item] }, { ...trash, items: [{ ...item, deleted_at: -1 }] },
    { ...trash, items: [{ ...item, purge_at: "2030" }] }, { ...trash, items: [{ ...item, deletion_id: 1 }] }]) {
    const mocked = context.mock.method(globalThis, "fetch", async () => Response.json(value));
    await assert.rejects(readTrash("synthetic", new AbortController().signal), /Invalid Trash/); mocked.mock.restore();
  }
  assert.equal(validTrashItem({ ...item, purge_at: Infinity }), false);
});

test("restore and purge send exact captured deletion generations, with no implicit selection request", async (context) => {
  const calls: string[] = [];
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    calls.push(`${options.method} ${url}`);
    assert.deepEqual(JSON.parse(String(options.body)), { deletion_id: item.deletion_id });
    return Response.json(options.method === "POST" ? { restored_session_id: item.id, sessions: [session] } : { purged_session_id: item.id });
  });
  assert.equal((await restoreTrash("synthetic", item)).restored_session_id, item.id);
  await purgeTrash("synthetic", item);
  assert.deepEqual(calls, [`POST /api/trash/${item.id}/restore`, `DELETE /api/trash/${item.id}`]);
});

test("restore/purge mismatched acknowledgements and stale/expired generations are not retried", async (context) => {
  for (const status of [200, 409, 410]) {
    let calls = 0;
    const mocked = context.mock.method(globalThis, "fetch", async () => { calls++; return Response.json({ detail: "stale generation", restored_session_id: "ngn-wrong", purged_session_id: "ngn-wrong", sessions: [] }, { status }); });
    await assert.rejects(restoreTrash("synthetic", item)); await assert.rejects(purgeTrash("synthetic", item));
    assert.equal(calls, 2); mocked.mock.restore();
  }
});

function fixture() {
  const calls: string[] = [];
  const timers: { callback: () => void; cancelled: boolean }[] = [];
  const deps: TrashDependencies = {
    read: async () => { calls.push("read"); return trash; },
    save: async (revision, days) => { calls.push(`save:${revision}:${days}`); return { ...trash, retention_days: Number(days) }; },
    softDelete: async (id) => { calls.push(`soft:${id}`); return item; },
    permanent: async (id) => { calls.push(`permanent:${id}`); },
    restore: async (captured) => { calls.push(`restore:${captured.deletion_id}`); return { restored_session_id: captured.id, sessions: [session] }; },
    purge: async (captured) => { calls.push(`purge:${captured.deletion_id}`); },
    restored: (reply) => { calls.push(`listed:${reply.restored_session_id}`); },
    mutate: (action) => action(),
    schedule: (callback) => { const timer = { callback, cancelled: false }; timers.push(timer); return () => { timer.cancelled = true; }; },
  };
  const controller = new TrashController(deps);
  return { controller, calls, deps, timers };
}

test("one click moves immediately; pending blocks double admission; Undo restores the captured generation to the list only", async () => {
  const f = fixture(); const pending = deferred<TrashItem>();
  f.deps.softDelete = async () => { f.calls.push("soft"); return pending.promise; };
  const moving = f.controller.move(session);
  assert.deepEqual(f.calls, ["soft"]); assert.equal(f.controller.getSnapshot().pending, item.id);
  await assert.rejects(f.controller.move(session), /Finish/);
  pending.resolve(item); await moving;
  assert.equal(f.controller.getSnapshot().notice?.item?.deletion_id, item.deletion_id);
  await f.controller.restore(f.controller.getSnapshot().notice!.item!);
  assert.deepEqual(f.calls, ["soft", `restore:${item.deletion_id}`, `listed:${item.id}`]);
  assert.equal(f.controller.getSnapshot().notice?.status, "restored");
  f.controller.dispose();
});

test("soft delete failure keeps the current list and draft callbacks untouched and displays an actionable error", async () => {
  const f = fixture(); f.deps.softDelete = async () => { throw new RequestError("Channel is bound", 409); };
  await assert.rejects(f.controller.move(session), /bound/);
  assert.equal(f.controller.getSnapshot().notice, undefined);
  assert.match(f.controller.getSnapshot().error, /bound/); assert.deepEqual(f.calls, ["read"]);
});

test("expired Undo and stale purge refetch Trash even while closed, retaining exact old IDs and never retrying a write", async () => {
  for (const code of [409, 410]) {
    const f = fixture();
    f.deps.restore = async (captured) => { f.calls.push(captured.deletion_id); throw new RequestError("Expired or changed", code); };
    f.deps.read = async () => { f.calls.push("refresh"); return { ...trash, items: [{ ...item, deletion_id: "new-generation" }] }; };
    await assert.rejects(f.controller.restore(item));
    assert.deepEqual(f.calls, [item.deletion_id, "refresh"]);
    assert.equal(f.controller.getSnapshot().snapshot?.items[0].deletion_id, "new-generation");
    assert.equal(item.deletion_id, "opaque-generation-a"); f.controller.dispose();
  }
});

test("permanent deletion requires explicit confirmation and captures a Trash generation through cancel/failure/success", async () => {
  const f = fixture();
  const confirmation = new SessionDeletion(() => {}, async (id, captured) => { await f.controller.permanent(id, captured); return true; });
  confirmation.showTrash(item); assert.deepEqual(f.calls, []);
  confirmation.cancel(); await confirmation.confirm(); assert.deepEqual(f.calls, []);
  confirmation.showTrash(item); await confirmation.confirm();
  assert.deepEqual(f.calls, [`purge:${item.deletion_id}`]);
  confirmation.show(session); await confirmation.confirm();
  assert.deepEqual(f.calls, [`purge:${item.deletion_id}`, `permanent:${item.id}`]);
});

test("retention edit cancellation and policy changes never alter existing per-row purge deadlines", async () => {
  const f = fixture(); f.controller.show(); await f.controller.refresh();
  f.controller.editDays("7"); f.controller.cancelEdit();
  assert.equal(f.controller.getSnapshot().draftDays, "30");
  f.controller.editDays("7"); await f.controller.save();
  assert.ok(f.calls.includes(`save:${trash.revision}:7`));
  assert.equal(f.controller.getSnapshot().snapshot?.items[0].purge_at, item.purge_at);
  f.controller.editDays("366"); await f.controller.save(); assert.match(f.controller.getSnapshot().error, /1 to 365/);
  f.controller.close(); assert.equal(f.timers.filter((timer) => !timer.cancelled).length, 0);
});

test("periodic refresh keeps the edit's original revision so concurrent policy changes produce a reviewable conflict", async () => {
  const f = fixture(); f.controller.show(); await f.controller.refresh(); f.controller.editDays("7");
  f.deps.read = async () => ({ ...trash, revision: "new-policy", retention_days: 90 });
  await f.controller.refresh();
  f.deps.save = async (revision) => { assert.equal(revision, trash.revision); throw new RequestError("Policy changed", 409); };
  await assert.rejects(f.controller.save(), /Policy changed/);
  assert.equal(f.controller.getSnapshot().draftDays, "7");
  assert.equal(f.controller.getSnapshot().snapshot?.revision, "new-policy"); f.controller.dispose();
});

test("open polling owns one cancellable read/timer; close and dispose ignore late noncooperative responses", async () => {
  const f = fixture(); const pending = deferred<TrashSnapshot>(); let signal: AbortSignal | undefined;
  f.deps.read = async (readSignal) => { signal = readSignal; return pending.promise; };
  f.controller.show(); assert.equal(signal?.aborted, false);
  f.controller.close(); assert.equal(signal?.aborted, true);
  pending.resolve(trash); await Promise.resolve(); await Promise.resolve();
  assert.equal(f.controller.getSnapshot().snapshot, undefined);
  assert.equal(f.controller.getSnapshot().loading, false);
  f.deps.read = async () => trash; f.controller.show(); await f.controller.refresh();
  assert.equal(f.timers.filter((timer) => !timer.cancelled).length, 1);
  f.controller.dispose(); assert.ok(f.timers.every((timer) => timer.cancelled));
});

test("sidebar exposes one-click Trash separately from a top-layer More actions menu and permanent confirmation", () => {
  const unexpected = () => assert.fail("Rendering cannot mutate sessions");
  const html = renderToStaticMarkup(createElement(SessionSidebar, { workspace: "/fixture", sessions: [session], selected: session.id,
    disabled: false, open: false, select: unexpected, remove: unexpected, permanent: unexpected, canDelete: () => true,
    trash: unexpected, trashDisabled: false, settings: unexpected, settingsDisabled: false, channels: unexpected, channelsDisabled: false, demo: true, close: unexpected }));
  assert.match(html, /aria-label="Move to Trash: Soft deleted conversation"/);
  assert.doesNotMatch(html.match(/<button class="session-delete"[^>]*>/)?.[0] || "", /aria-haspopup/);
  assert.match(html, /aria-haspopup="menu"/); assert.match(html, /popover="auto"/); assert.match(html, /role="menuitem"/);
  assert.match(html, /Delete forever…/); assert.match(html, /class="sidebar-utility trash-trigger"/);
  const confirmation = new SessionDeletion(() => {}, async () => true); confirmation.showTrash(item);
  assert.match(renderToStaticMarkup(createElement(DeleteSessionDialog, { state: confirmation.state, controller: confirmation })), /Delete forever\?/);
});

test("Trash panel renders retention bounds, frozen dates, Restore and permanent actions with no mutation on mount", () => {
  const f = fixture();
  const html = renderToStaticMarkup(createElement(TrashDialog, { state: { ...f.controller.getSnapshot(), snapshot: trash }, controller: f.controller,
    permanent: () => assert.fail("render cannot purge"), openSession: () => assert.fail("render cannot resume"), blocked: false }));
  assert.match(html, /min="1" max="365" step="1"/); assert.match(html, /dates already shown below stay the same/);
  assert.match(html, />Restore<\/button>/); assert.match(html, /Delete forever…/); assert.match(html, /Close Trash/);
  assert.deepEqual(f.calls, []);
});

test("row menu supports Arrow/Home/End/Escape/Tab and stays inside small viewports near scroller edges", () => {
  for (const key of ["ArrowDown", "ArrowUp", "Home", "End"]) assert.equal(menuKey(key), "focus");
  assert.equal(menuKey("Escape"), "close"); assert.equal(menuKey("Tab"), "tab"); assert.equal(menuKey("x"), "");
  const position = menuPosition({ left: 200, right: 245, top: 740, bottom: 784 }, 390, 800);
  assert.ok(position.left >= 8 && position.left + 212 <= 382 && position.top + 60 < 800);
  assert.match(renderToStaticMarkup(createElement(SessionMenu, { session, disabled: false, permanent() {} })), /aria-expanded="false"/);
});

test("Escape stops propagation before closing More actions, leaving the document-level mobile drawer open", () => {
  const sequence: string[] = [];
  let stopped = false, menuOpen = true, drawerOpen = true;
  handleMenuKey({ key: "Escape", stopPropagation() { stopped = true; sequence.push("stop"); },
    preventDefault() { sequence.push("prevent"); } },
    () => assert.fail("Escape cannot navigate menu items"),
    () => { assert.equal(stopped, true); menuOpen = false; sequence.push("close-and-focus-trigger"); });
  // Model bubbling into the existing drawer listener after the target handler.
  if (!stopped && !menuOpen) drawerOpen = false;
  assert.equal(drawerOpen, true); assert.equal(menuOpen, false);
  assert.deepEqual(sequence, ["stop", "prevent", "close-and-focus-trigger"]);
});

test("menu arrows retain focus while Tab closes to its trigger without cancelling native forward/backward traversal", () => {
  for (const key of ["ArrowDown", "ArrowUp", "Home", "End", "Tab"]) {
    const calls: string[] = [];
    handleMenuKey({ key, stopPropagation: () => calls.push("stop"), preventDefault: () => calls.push("prevent") },
      () => calls.push("focus-item"), () => calls.push("close-and-focus-trigger"));
    assert.deepEqual(calls, key === "Tab" ? ["stop", "close-and-focus-trigger"] : ["stop", "prevent", "focus-item"]);
  }
});
