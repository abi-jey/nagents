import type { EventFrame } from "../../api/subscription.js";
import type { ActiveRun } from "../../types.js";

export type HarnessActivity = { id: string; sessionId: string; busy: boolean; status?: string };
export const idle: HarnessActivity = { id: "", sessionId: "", busy: false };
export type SessionActivities = Record<string, HarnessActivity>;
const activeStatus = (status = "") => ["running", "working", "active", "approval", "waiting_for_approval", "cancelling"].includes(status);
export function activitiesFromRuns(runs: ActiveRun[]): SessionActivities {
  return Object.fromEntries(runs.filter(run => run.session_id && activeStatus(run.status)).map(run =>
    [run.session_id!, { id: run.id, sessionId: run.session_id!, busy: true, status: run.status }]));
}
export function sessionActivities(current: SessionActivities, frame: EventFrame): SessionActivities {
  const runs = frame.type === "status" ? frame.active_runs : frame.type === "snapshot" ? frame.snapshot.active_runs : undefined;
  if (runs !== undefined) return activitiesFromRuns(runs);
  if (frame.type === "status") {
    return frame.active_run_id ? { [frame.active_session_id]: { id: frame.active_run_id, sessionId: frame.active_session_id, busy: true } } : {};
  }
  if (frame.type === "sessions") {
    const roots = new Set(frame.sessions.map(session => session.id));
    return Object.fromEntries(Object.entries(current).filter(([root]) => roots.has(root)));
  }
  const next = { ...current };
  if (frame.type === "event") {
    if (frame.record.event === "run_started" && typeof frame.record.run_id === "string")
      next[frame.session_id] = { id: frame.record.run_id, sessionId: frame.session_id, busy: true };
    if (frame.record.event === "run_finished" && next[frame.session_id]?.id === frame.record.run_id) delete next[frame.session_id];
  } else {
    const run = frame.snapshot.active_run;
    if (run && activeStatus(run.status)) next[frame.session_id] = { id: run.id, sessionId: frame.session_id, busy: true };
    else if ("active_run" in frame.snapshot) delete next[frame.session_id];
  }
  return next;
}
export function sessionActivity(current: HarnessActivity, frame: EventFrame): HarnessActivity {
  if (frame.type === "status") return { id: frame.active_run_id, sessionId: frame.active_session_id, busy: !!frame.active_run_id };
  if (frame.type === "event") {
    if (frame.record.event === "run_started" && typeof frame.record.run_id === "string")
      return { id: frame.record.run_id, sessionId: frame.session_id, busy: true };
    if (frame.record.event === "run_finished" && current.id === frame.record.run_id) return idle;
    return current;
  }
  const list = frame.type === "snapshot" ? frame.snapshot.sessions : frame.sessions;
  const run = frame.type === "snapshot" ? frame.snapshot.active_run : frame.active_run;
  const running = list.find((session) => session.active_run_id || activeStatus(session.status));
  if (run && !["completed", "failed", "cancelled", "interrupted"].includes(run.status))
    return { id: run.id, sessionId: run.session_id || (frame.type === "snapshot" ? frame.session_id : running?.id || ""), busy: true };
  if (running) return { id: running.active_run_id || "", sessionId: running.id, busy: true };
  if (frame.type === "snapshot" && frame.snapshot.active_run_id)
    return { id: frame.snapshot.active_run_id, sessionId: frame.snapshot.active_session_id || frame.session_id, busy: true };
  if (frame.type === "snapshot" && "active_run_id" in frame.snapshot && "active_session_id" in frame.snapshot) return idle;
  // A global catalog with runtime fields is authoritative across all roots.
  if (frame.type === "sessions" && ("active_run" in frame || list.some((session) => "active_run_id" in session || "status" in session))) return idle;
  // An idle selected-root snapshot says nothing about another root's run.
  if (frame.type === "snapshot" && ("active_run" in frame.snapshot || "active_run_id" in frame.snapshot) && current.sessionId === frame.session_id) return idle;
  return current;
}
