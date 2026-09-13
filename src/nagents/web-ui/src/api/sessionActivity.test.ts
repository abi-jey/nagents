import assert from "node:assert/strict";
import test from "node:test";
import { idle, sessionActivity } from "../features/sessions/activity.js";

test("an idle selected snapshot does not erase another root's running producer", () => {
  const busy = { id: "run-other", sessionId: "other", busy: true };
  assert.deepEqual(sessionActivity(busy, { type: "snapshot", cursor: 1, epoch: "epoch", session_id: "selected",
    snapshot: { session_id: "selected", sessions: [], retained_tasks: [], history: [], active_run: null } }), busy);
  assert.deepEqual(sessionActivity(busy, { type: "event", cursor: 2, epoch: "epoch", session_id: "selected",
    record: { event: "run_finished", run_id: "different-run" } }), busy);
});
test("global status and optional session runtime fields track work independently of the selected root", () => {
  let state = sessionActivity(idle, { type: "status", cursor: 1, epoch: "e", active_session_id: "telegram", active_run_id: "run" });
  assert.deepEqual(state, { id: "run", sessionId: "telegram", busy: true });
  state = sessionActivity(state, { type: "sessions", cursor: 2, epoch: "e", sessions: [
    { id: "telegram", title: "New Telegram chat", updated_at: "now", status: "approval" },
  ] });
  assert.equal(state.busy, true); assert.equal(state.sessionId, "telegram");
  state = sessionActivity(state, { type: "status", cursor: 3, epoch: "e", active_run_id: "", active_session_id: "" });
  assert.deepEqual(state, idle);
});
test("global active snapshot fields clear another root's stale busy state after a lost run-finished frame", () => {
  const state = sessionActivity({ id: "old", sessionId: "other", busy: true }, {
    type: "snapshot", cursor: 3, epoch: "e", session_id: "selected", snapshot: {
      session_id: "selected", sessions: [], history: [], retained_tasks: [], active_run: null, active_run_id: "", active_session_id: "",
    },
  });
  assert.deepEqual(state, idle);
});
