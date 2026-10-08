import type { SphereEvent } from "../../components/voiceSphere/useVoiceSphere.js";
import type { LiveDelegation, LiveDelegationStatus } from "./types.js";

export type LiveSphereDispatch = (event: SphereEvent) => boolean;

export interface LiveSphereEvents {
  /** Accept current backend records; repeated polls and stale sessions are inert. */
  reconcile(sessionId: string, records: readonly LiveDelegation[], dispatch: LiveSphereDispatch): void;
  /** Retry queued starts after an animation frees one of the three visual slots. */
  flush(dispatch: LiveSphereDispatch): void;
  /** Forget this engine generation. The next reconcile resets its visual task state. */
  reset(): void;
}

interface Entry {
  seq: number;
  status: LiveDelegationStatus;
  chatSessionId: string;
  runId: string;
  label: string;
  started: boolean;
  settled: boolean;
}

const terminal = (status: LiveDelegationStatus): status is "completed" | "failed" | "cancelled" => status === "completed" || status === "failed" || status === "cancelled";
const statuses = new Set<LiveDelegationStatus>(["queued", "working", "completed", "failed", "cancelled"]);
const settledHistoryLimit = 64;

/** Converts observed Live lifecycle changes into bounded visual events, never backend work. */
export function createLiveSphereEvents(): LiveSphereEvents {
  let session = "", resetPending = true, flushing = false, retiredThrough = -1;
  const entries = new Map<string, Entry>();

  function trimHistory(): void {
    // Active work and unacknowledged terminal events must survive any history churn.
    const settled = [...entries].filter(([, entry]) => entry.settled && terminal(entry.status)).sort((a, b) => a[1].seq - b[1].seq);
    for (const [id, entry] of settled.slice(0, Math.max(0, settled.length - settledHistoryLimit))) {
      retiredThrough = Math.max(retiredThrough, entry.seq);
      entries.delete(id);
    }
  }

  function flush(dispatch: LiveSphereDispatch): void {
    // dispatch may synchronously publish a snapshot which asks us to flush again.
    if (flushing) return;
    flushing = true;
    try {
      if (resetPending) {
        if (!dispatch({ type: "delegations-reset" })) return;
        resetPending = false;
      }
      if (!session) return;
      // Failures/cancellations release slots before waiting active records are retried.
      for (const [id, entry] of entries) {
        if (!terminal(entry.status) || entry.settled) continue;
        if (!entry.started) { entry.settled = true; continue; }
        const event: SphereEvent = entry.status === "completed"
          ? { type: "delegation-result", id }
          : { type: "delegation-finished", id, outcome: entry.status };
        if (dispatch(event)) entry.settled = true;
      }
      for (const [id, entry] of entries) {
        if (entry.started || entry.settled || terminal(entry.status)) continue;
        if (dispatch({ type: "delegation-start", id, label: entry.label })) entry.started = true;
      }
    } finally { trimHistory(); flushing = false; }
  }

  return {
    reconcile(sessionId, records, dispatch) {
      if (sessionId !== session) { session = sessionId; entries.clear(); retiredThrough = -1; resetPending = true; }
      if (session) {
        const ordered = records.filter(record => record.sessionId === session && record.id.trim() && Number.isSafeInteger(record.seq) && record.seq >= 0 && statuses.has(record.status)).slice().sort((a, b) => a.seq - b.seq);
        for (const record of ordered) {
          const id = record.id.trim(), previous = entries.get(id);
          // A forgotten record older than this floor is replayed history. Existing
          // active IDs bypass the floor so late terminal snapshots can still settle them.
          if (!previous && record.seq <= retiredThrough) continue;
          if (previous && (record.seq <= previous.seq || terminal(previous.status) ||
              record.chatSessionId !== previous.chatSessionId || (previous.runId && record.runId !== previous.runId) ||
              (previous.status === "working" && record.status === "queued"))) continue;
          entries.set(id, {
            seq: record.seq, status: record.status, chatSessionId: record.chatSessionId, runId: record.runId,
            label: record.agent.trim() || "Assistant", started: previous?.started ?? false,
            // History first seen as terminal must not manufacture a fresh launch.
            settled: previous?.settled ?? terminal(record.status),
          });
        }
      }
      flush(dispatch);
    },
    flush,
    reset() { session = ""; entries.clear(); retiredThrough = -1; resetPending = true; },
  };
}
