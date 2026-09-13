import type { ActiveRun, Snapshot, WireEvent } from "../../types.js";
import type { Position, SessionFrame } from "../../api/subscription.js";
import type { QueuedMessage } from "../../api/messages.js";
import { appendEvent, fromHistory, sameTranscriptUser, type Entry } from "./transcript.js";

export type LiveTranscript = {
  entries: Entry[];
  position?: Position;
  activeRun?: ActiveRun;
  history?: Snapshot["history"];
  runHistory?: { runId: string; history: Snapshot["history"] };
};
const finished = (status: string) => ["completed", "cancelled", "failed", "interrupted"].includes(status);
export function pendingApprovals(run?: ActiveRun): WireEvent[] {
  const pending = new Map<string, WireEvent>();
  for (const event of run?.events || []) {
    if (event.event === "approval" && typeof event.approval_id === "string") pending.set(event.approval_id, event);
    if (event.event === "approval_closed" && typeof event.approval_id === "string") pending.delete(event.approval_id);
  }
  for (const event of [...(run?.pending_approvals || []), ...(run?.approval ? [run.approval] : [])])
    if (typeof event.approval_id === "string") pending.set(event.approval_id, { ...event, event: "approval", run_id: event.run_id || run?.id });
  return [...pending.values()];
}
function compatible(saved: Entry, previous: Entry): boolean {
  if (saved.kind !== previous.kind || previous.taskId || (previous.recorded && previous.kind !== "context")) return false;
  return saved.kind !== "tool" || saved.callId === previous.callId;
}

function rowIds(entry: Entry): string[] { return [entry.historyId, entry.resultHistoryId].filter((id): id is string => !!id); }
function conflictingRows(left: Entry, right: Entry): boolean {
  const a = rowIds(left), b = rowIds(right);
  return !!a.length && !!b.length && !a.some((id) => b.includes(id));
}
function sameSavedEntry(saved: Entry, previous: Entry): boolean {
  if (!compatible(saved, previous)) return false;
  if (saved.kind === "user" && sameTranscriptUser(saved, previous)) return true;
  const a = rowIds(saved), b = rowIds(previous);
  if (a.length && b.length) return a.some((id) => b.includes(id));
  if (a.length || b.length || saved.messageId || previous.messageId || saved.ingressId || previous.ingressId || saved.originId || previous.originId) return false;
  return saved.historyIndex !== undefined && saved.historyIndex === previous.historyIndex;
}
function sameTurn(left?: Entry, right?: Entry): boolean {
  return !!left && !!right && (sameTranscriptUser(left, right) || sameSavedEntry(left, right));
}
function turnOwners(entries: Entry[]): Map<Entry, Entry> {
  const owners = new Map<Entry, Entry>();
  const inputs = new Map<string, Entry>();
  let savedInput: Entry | undefined;
  for (const entry of entries) {
    if (!entry.taskId && (entry.kind === "user" || entry.kind === "context")) {
      if (entry.historyIndex !== undefined) savedInput = entry;
      if (entry.runId && entry.kind === "user") inputs.set(entry.runId, entry);
    } else {
      // A wakeup/unlinked run must not inherit the last unrelated saved user.
      const owner = entry.runId ? inputs.get(entry.runId) : savedInput;
      if (owner) owners.set(entry, owner);
    }
  }
  return owners;
}
function newRow(entry: Entry, history?: Snapshot["history"]): boolean {
  if (!history) return false;
  const ids = rowIds(entry);
  return ids.length ? ids.some((id) => !history.some((row) => row.history_id === id))
    : entry.historyIndex !== undefined && entry.historyIndex >= history.length;
}

// History IDs identify saved rows; ingress/message IDs identify user turns.
// A live response can join only its own turn, in order. Text/prefix equality is
// never an identity, and an unfinished stream cannot consume an existing row.
export function reconcileHistory(previous: Entry[], snapshot: Snapshot, announceNewUsers = false,
  knownHistory?: Snapshot["history"], settledRun?: LiveTranscript["runHistory"]): Entry[] {
  const saved = fromHistory(snapshot);
  const roots = saved.filter((item) => item.kind !== "task" && item.kind !== "retained_tasks");
  const oldOwners = turnOwners(previous), savedOwners = turnOwners(roots);
  const matched = new Set<number>();
  const turnPositions = new Map<Entry, number>();
  const matches = roots.map((entry) => {
    const owner = savedOwners.get(entry);
    let index = previous.findIndex((item, index) => !matched.has(index) && sameSavedEntry(entry, item));
    // Only after an unlinked run finishes may its output join rows added since
    // that run's history baseline. Also consumes any provisional saved copy.
    if (settledRun && entry.kind !== "user" && newRow(entry, settledRun.history)) {
      const pending = previous.findIndex((item, i) => !matched.has(i) && compatible(entry, item) &&
        item.runId === settledRun.runId && !item.historyId && !oldOwners.has(item));
      if (pending >= 0) { if (index >= 0) matched.add(index); index = pending; }
    }
    if (index < 0 && entry.kind !== "user" && owner) {
      index = previous.findIndex((item, i) => !matched.has(i) && i > (turnPositions.get(owner) ?? -1) &&
        compatible(entry, item) && !conflictingRows(entry, item) && sameTurn(owner, oldOwners.get(item)) &&
        (!item.streaming || newRow(entry, knownHistory)));
    }
    if (index >= 0) {
      matched.add(index);
      if (owner) turnPositions.set(owner, index);
    }
    return index;
  });
  const matchedIds = new Set([...matched].map((index) => previous[index].id));
  const next: Entry[] = [];
  const used = new Set<string>();
  let after = 0;
  let serial = 0;
  const id = () => {
    let candidate: string;
    do { candidate = `saved-${serial++}`; } while (previous.some((item) => item.id === candidate) || next.some((item) => item.id === candidate));
    return candidate;
  };
  function keep(entry: Entry) {
    if (used.has(entry.id) || matchedIds.has(entry.id)) return;
    if (entry.runId || entry.queued || (!entry.recorded && entry.taskId)) { next.push(entry); used.add(entry.id); }
  }
  for (const [position, entry] of roots.entries()) {
    const index = matches[position];
    if (index < 0) {
      const activity = entry.origin && announceNewUsers ? [...previous, ...next].reduce((latest, item) => Math.max(latest, item.activity || 0), 0) + 1 : entry.activity;
      next.push({ ...entry, id: id(), activity }); continue;
    }
    for (let i = after; i < index; i++) keep(previous[i]);
    const old = previous[index];
    next.push(entry.kind === "user"
      ? { ...old, ...entry, id: old.id, runId: old.runId, queued: false, activity: old.activity,
          origin: entry.origin, originId: entry.originId, provenance: entry.provenance, channelContext: entry.channelContext }
      : { ...entry, ...old, historyId: entry.historyId, resultHistoryId: entry.resultHistoryId,
          historyIndex: entry.historyIndex, historyTurn: entry.historyTurn,
          text: entry.kind === "assistant" ? entry.text : old.text,
          streaming: entry.kind === "assistant" ? false : old.streaming,
          result: entry.result ?? old.result, queued: false });
    used.add(old.id); after = Math.max(after, index + 1);
  }
  for (let i = after; i < previous.length; i++) keep(previous[i]);
  for (const entry of saved.filter((item) => item.kind === "task" || item.kind === "retained_tasks")) {
    const old = previous.find((item) => item.recorded && item.kind === entry.kind && item.taskId === entry.taskId);
    next.push({ ...entry, id: old?.id || id() });
  }
  return next;
}

export function applySnapshot(current: LiveTranscript, snapshot: Snapshot, announceUpdates = current.history !== undefined): LiveTranscript {
  let entries = current.entries;
  const supplied = snapshot.active_run;
  const run = supplied && !finished(supplied.status) && (!supplied.session_id || supplied.session_id === snapshot.session_id)
    ? supplied : undefined;
  const runHistory = run && current.runHistory?.runId !== run.id ? { runId: run.id, history: snapshot.history } : current.runHistory;
  if (run?.events) {
    let replay: Entry[] = [];
    for (const record of run.events) replay = appendEvent(replay, {
      ...record, run_id: record.run_id || run.id, ...(record.event === "user_message" ? { saved: true } : {}),
    });
    for (const record of pendingApprovals(run)) replay = appendEvent(replay, record);
    // Replace the active run's replayable records, rather than append chunks again.
    // Map equivalent scopes back to stable local IDs for expanded disclosures.
    const claimed = new Set<string>();
    const oldOwners = turnOwners(entries), replayOwners = turnOwners(replay);
    const positions = new Map<Entry, number>();
    replay = replay.map((entry, index) => {
      const owner = replayOwners.get(entry);
      const scoped = entries.find((item) => !claimed.has(item.id) && item.runId === entry.runId && item.taskId === entry.taskId &&
        item.activation === entry.activation && (item.followup || 0) === (entry.followup || 0) &&
        item.kind === entry.kind && item.callId === entry.callId && !conflictingRows(entry, item) &&
        (!entry.streaming || !!item.streaming) &&
        (entry.kind !== "user" || sameTranscriptUser(entry, item)) &&
        (!owner || !oldOwners.has(item) || sameTurn(owner, oldOwners.get(item))));
      // A replay can claim saved completed slots only within its identified user
      // turn. In particular, a partial chunk never borrows an old answer's ID.
      const old = scoped || entries.find((item, i) => !claimed.has(item.id) && !item.runId && compatible(entry, item) &&
        (entry.kind === "user" ? sameTranscriptUser(entry, item)
          : !entry.streaming && !!owner && i > (positions.get(owner) ?? -1) &&
            !conflictingRows(entry, item) && sameTurn(owner, oldOwners.get(item))));
      if (old) { claimed.add(old.id); if (owner) positions.set(owner, entries.indexOf(old)); }
      return { ...entry, id: old?.id || `run:${run.id}:${index}`,
        historyId: entry.historyId || old?.historyId, resultHistoryId: entry.resultHistoryId || old?.resultHistoryId,
        historyIndex: old?.historyIndex, historyTurn: old?.historyTurn,
      };
    });
    const first = entries.findIndex((entry) => entry.runId === run.id || claimed.has(entry.id));
    const before = first < 0 ? entries : entries.slice(0, first);
    entries = [...before.filter((entry) => entry.runId !== run.id && !claimed.has(entry.id)), ...replay,
      ...(first < 0 ? [] : entries.slice(first).filter((entry) => entry.runId !== run.id && !claimed.has(entry.id)))];
  }
  // A first/explicit history load establishes a baseline, even if React renders
  // it after the selected session mounts. Only later snapshot additions announce
  // source activity; matching saved rows retain their existing activity counter.
  entries = reconcileHistory(entries, snapshot, announceUpdates, current.history, !run ? runHistory : undefined);
  if (run) entries = appendEvent(entries, { event: "run_started", run_id: run.id,
    history_id: run.history_id, ingress_id: run.ingress_id, message_id: run.message_id,
  });
  if (current.activeRun && !run) entries = appendEvent(entries, {
    event: "run_finished", run_id: current.activeRun.id, status: snapshot.active_run?.status || "unknown",
  });
  if (run && !run.events) for (const record of pendingApprovals(run)) entries = appendEvent(entries, record);
  // The backend's `records` are compact, aggregated *active* chunks, rather than
  // a full event log. Replace only those streams, retaining observed tool/task
  // evidence, provisional approvals and earlier activations in this connection.
  for (const record of run?.records || []) {
    if (record.event !== "text_chunk") continue;
    const event = { ...record, run_id: record.run_id || run?.id, event: "text_done", text: record.chunk };
    entries = appendEvent(entries, event);
    const index = entries.findLastIndex((entry) => entry.kind === "assistant" && entry.runId === event.run_id &&
      (entry.taskId || "") === (record.task_id || "") && (entry.activation || 0) === (record.activation || 0) &&
      (entry.followup || 0) === (record.followup || 0));
    entries = entries.map((entry, i) => i === index ? { ...entry, streaming: true } : entry);
  }
  return { ...current, entries, history: snapshot.history, activeRun: run, runHistory };
}

export function applyFrame(current: LiveTranscript, frame: SessionFrame): LiveTranscript {
  if (current.position?.epoch === frame.epoch && (frame.cursor < current.position.cursor || (frame.type === "event" && frame.cursor === current.position.cursor))) return current;
  if (frame.type === "event" && current.position && current.position.epoch !== frame.epoch) return current;
  let next = current;
  if (frame.type === "snapshot") next = applySnapshot(current, frame.snapshot);
  else {
    const event = frame.record;
    let activeRun = current.activeRun;
    let runHistory = current.runHistory;
    if (event.event === "run_started" && typeof event.run_id === "string") {
      activeRun = { id: event.run_id, status: "running" };
      runHistory = current.history ? { runId: event.run_id, history: current.history } : undefined;
    }
    if (event.event === "approval" && !activeRun && typeof event.run_id === "string") activeRun = { id: event.run_id, status: "approval" };
    if (event.event === "run_finished" && event.run_id === activeRun?.id) activeRun = undefined;
    if (event.event === "approval" && activeRun) activeRun = { ...activeRun, pending_approvals: [...pendingApprovals(activeRun), event] };
    if (event.event === "approval_closed" && activeRun) activeRun = { ...activeRun, pending_approvals: pendingApprovals(activeRun).filter((item) => item.approval_id !== event.approval_id), events: undefined, approval: undefined };
    next = { ...current, activeRun, runHistory, entries: appendEvent(current.entries, event) };
  }
  return { ...next, position: { cursor: frame.cursor, epoch: frame.epoch } };
}

export class LiveSessions {
  private roots = new Map<string, LiveTranscript>();
  get(id: string): LiveTranscript { return this.roots.get(id) || { entries: [] }; }
  set(id: string, state: LiveTranscript): LiveTranscript { this.roots.set(id, state); return state; }
  receive(frame: SessionFrame): LiveTranscript {
    return this.set(frame.session_id, applyFrame(this.get(frame.session_id), frame));
  }
  enqueue(message: QueuedMessage): LiveTranscript {
    const state = this.get(message.session_id);
    return this.set(message.session_id, { ...state, entries: appendEvent(state.entries, {
      event: "user_message", text: message.prompt, message_id: message.message_id, queued: true,
    }) });
  }
}
