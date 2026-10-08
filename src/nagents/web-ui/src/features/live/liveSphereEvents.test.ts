import assert from "node:assert/strict";
import test from "node:test";
import { createSphereEngine } from "../../components/voiceSphere/engine.js";
import { silentSignal, type SphereConfig } from "../../components/voiceSphere/types.js";
import type { SphereEvent } from "../../components/voiceSphere/useVoiceSphere.js";
import { createLiveSphereEvents, type LiveSphereDispatch } from "./liveSphereEvents.js";
import type { LiveDelegation, LiveDelegationStatus } from "./types.js";

const config: SphereConfig = { mode: "listen", playing: true, density: 3, speed: 1, thinkMin: .45, thinkMax: 1.65, glow: .65, color: "mint", dark: true, reducedMotion: false };

function record(id: string, seq: number, status: LiveDelegationStatus = "working", values: Partial<LiveDelegation> = {}): LiveDelegation {
  return { id, seq, status, sessionId: "live-a", chatSessionId: "chat-a", agent: id.toUpperCase(), provider: "openai", model: "test", runId: "run-" + id, text: "", ...values };
}

function fixture() {
  const engine = createSphereEngine(config), bridge = createLiveSphereEvents();
  const events: { event: SphereEvent; accepted: boolean }[] = [];
  let reenter = false;
  const dispatch: LiveSphereDispatch = event => {
    let accepted = true;
    switch (event.type) {
      case "delegations-reset": engine.resetDelegations(); break;
      case "delegation-start": accepted = engine.startDelegation(event.id, event.label); break;
      case "delegation-result": accepted = engine.deliverResult(event.id); break;
      case "delegation-finished": accepted = engine.finishDelegation(event.id, event.outcome); break;
      case "pulse": engine.pulse(event.node); break;
      case "view-reset": engine.resetView(); break;
    }
    events.push({ event, accepted });
    if (reenter) bridge.flush(dispatch);
    return accepted;
  };
  return {
    engine, bridge, dispatch, events,
    reenter: () => { reenter = true; },
    update: (records: LiveDelegation[], session = "live-a") => bridge.reconcile(session, records, dispatch),
    accepted: (type: SphereEvent["type"]) => events.filter(value => value.accepted && value.event.type === type).map(value => value.event),
    advance: (seconds: number, retryBridge = true) => {
      for (let elapsed = 0; elapsed < seconds; elapsed += .05) {
        engine.tick(.05, { input: silentSignal(), output: silentSignal() }); if (retryBridge) bridge.flush(dispatch);
      }
    },
  };
}

test("polled delegation snapshots launch once and queue a fast real result exactly once", () => {
  const f = fixture();
  try {
    f.reenter();
    f.update([record("a", 1, "queued")]); f.update([record("a", 1, "queued")]);
    f.update([record("a", 2)]); f.update([record("a", 3, "completed")]);
    assert.equal(f.accepted("delegation-start").length, 1);
    assert.equal(f.accepted("delegation-result").length, 1);
    assert.equal(f.engine.snapshot().tasks[0].phase, "launch");
    assert.equal(f.engine.snapshot().tasks[0].resultQueued, true);
    f.update([record("a", 2), record("a", 3, "completed"), record("a", 4, "failed")]);
    f.advance(4);
    assert.equal(f.engine.snapshot().tasks[0].phase, "complete");
    assert.equal(f.accepted("delegation-result").length, 1);
    assert.equal(f.accepted("delegation-finished").length, 0);
  } finally { f.engine.dispose(); }
});

test("terminal-only replay and historical full lifecycles never manufacture a launch", () => {
  const f = fixture();
  try {
    f.update([record("done", 9, "completed"), record("failed", 10, "failed"), record("cancelled", 11, "cancelled"), record("history", 2), record("history", 3, "completed")]);
    f.update([record("done", 1), record("failed", 99), record("history", 100)]);
    assert.equal(f.accepted("delegation-start").length, 0);
    assert.equal(f.accepted("delegation-result").length, 0);
    assert.deepEqual(f.engine.snapshot().tasks, []);
  } finally { f.engine.dispose(); }
});

test("stale status, foreign session/chat/run and invalid sequences cannot replace current work", () => {
  const f = fixture();
  try {
    f.update([record("a", 4), record("other", 1, "working", { sessionId: "old-live" }), record("invalid", NaN)]);
    f.update([record("a", 3, "completed"), record("a", 7, "queued"), record("a", 8, "failed", { chatSessionId: "foreign-chat" }), record("a", 9, "cancelled", { runId: "other-run" })]);
    assert.equal(f.accepted("delegation-start").length, 1);
    assert.equal(f.accepted("delegation-finished").length, 0);
    f.update([record("a", 5, "completed")]);
    assert.equal(f.accepted("delegation-result").length, 1);
  } finally { f.engine.dispose(); }
});

test("three-slot capacity retries pending records as real results and failures release space", () => {
  const f = fixture();
  try {
    const records = ["a", "b", "c", "d", "e"].map((id, i) => record(id, i + 1));
    f.update(records);
    assert.deepEqual(f.engine.snapshot().tasks.map(task => task.id), ["a", "b", "c"]);
    f.update([record("a", 10, "completed"), ...records.slice(1)]);
    assert.equal(f.accepted("delegation-start").length, 3, "A queued completion still occupies its visual slot");
    f.advance(4);
    assert.equal(f.accepted("delegation-start").length, 4);
    assert(f.engine.snapshot().tasks.some(task => task.id === "d"));
    f.update([record("b", 11, "failed"), ...records.slice(2)]);
    assert.equal(f.accepted("delegation-start").length, 5);
    assert(f.engine.snapshot().tasks.some(task => task.id === "e"));
    f.update([record("c", 12, "cancelled")]);
    assert.equal(f.engine.snapshot().tasks.find(task => task.id === "c")?.phase, "cancelled");
    assert.equal(f.accepted("delegation-finished").length, 2);
  } finally { f.engine.dispose(); }
});

test("a queued visual record that ends before receiving a slot is retired without a delayed fake start", () => {
  const f = fixture();
  try {
    f.update([record("a", 1), record("b", 2), record("c", 3), record("d", 4)]);
    f.update([record("d", 5, "completed"), record("a", 6, "failed")]);
    f.advance(2);
    assert.equal(f.accepted("delegation-start").length, 3);
    assert.equal(f.accepted("delegation-result").length, 0);
    assert(!f.engine.snapshot().tasks.some(task => task.id === "d"));
  } finally { f.engine.dispose(); }
});

test("session changes, unready engines and effect-generation resets isolate the visual lifecycle", () => {
  const f = fixture();
  try {
    f.bridge.reconcile("live-a", [record("a", 1)], () => false);
    assert.deepEqual(f.engine.snapshot().tasks, []);
    f.bridge.flush(f.dispatch); assert.equal(f.engine.snapshot().tasks[0].id, "a");
    f.update([record("a", 2, "completed"), record("b", 1, "working", { sessionId: "live-b" })], "live-b");
    assert.deepEqual(f.engine.snapshot().tasks.map(task => task.id), ["b"]);
    f.bridge.reset();
    f.update([record("b", 1, "working", { sessionId: "live-b" })], "live-b");
    assert.deepEqual(f.engine.snapshot().tasks.map(task => task.id), ["b"]);
    f.update([], ""); assert.deepEqual(f.engine.snapshot().tasks, []);
    assert.equal(f.accepted("delegation-result").length, 0);
  } finally { f.engine.dispose(); }
});

test("failed and cancelled engine tasks cannot turn into successful returns and release capacity immediately", () => {
  for (const outcome of ["failed", "cancelled"] as const) {
    const f = fixture();
    try {
      for (const id of ["a", "b", "c"]) assert(f.engine.startDelegation(id));
      assert.equal(f.engine.startDelegation("d"), false);
      assert(f.engine.deliverResult("a"));
      assert(f.engine.finishDelegation("a", outcome));
      assert.equal(f.engine.snapshot().tasks[0].phase, outcome); assert.equal(f.engine.snapshot().tasks[0].resultQueued, false);
      assert.equal(f.engine.deliverResult("a"), false); assert.equal(f.engine.finishDelegation("a", outcome), false);
      f.engine.configure({ ...config, density: 7 }); f.advance(4, false);
      assert.equal(f.engine.snapshot().tasks.find(task => task.id === "a")?.phase, outcome);
      assert(f.engine.startDelegation("d")); assert.equal(f.engine.startDelegation("a"), false);
      assert.equal(f.engine.finishDelegation("unknown", outcome), false);
    } finally { f.engine.dispose(); }
  }
});

test("terminal outcomes remain truthful in reduced motion and when cancelling a result already in transit", () => {
  const f = fixture();
  try {
    f.engine.startDelegation("returning"); f.advance(2, false); f.engine.deliverResult("returning");
    assert.equal(f.engine.snapshot().tasks[0].phase, "return");
    assert(f.engine.finishDelegation("returning", "cancelled"));
    f.engine.configure({ ...config, reducedMotion: true }); f.advance(2, false);
    assert.equal(f.engine.snapshot().tasks[0].phase, "cancelled");
    assert.match(f.engine.snapshot().delegationStatus, /cancelled/);
  } finally { f.engine.dispose(); }
});
