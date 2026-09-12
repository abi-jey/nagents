import type { ActiveRun, Snapshot, WireEvent } from "../../types.js";
import type { Position, SessionFrame } from "../../api/subscription.js";
import type { QueuedMessage } from "../../api/messages.js";
import { appendEvent, fromHistory, type Entry } from "./transcript.js";
import { sameUserMessage } from "./channelMessage.js";

export type LiveTranscript = {
  entries: Entry[];
  position?: Position;
  activeRun?: ActiveRun;
  history?: Snapshot["history"];
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
function same(saved: Entry, previous: Entry): boolean {
  if (saved.kind !== previous.kind || previous.taskId || (previous.recorded && previous.kind !== "context")) return false;
  if (saved.kind === "user") {
    if (sameUserMessage(saved, previous)) return true;
    return !saved.messageId && !previous.messageId && !saved.originId && !previous.originId &&
      saved.historyIndex !== undefined && saved.historyIndex === previous.historyIndex;
  }
  if (saved.kind === "tool") return saved.callId === previous.callId;
  return saved.text === previous.text || (saved.kind === "assistant" && !!previous.streaming && saved.text.startsWith(previous.text));
}

// Saved history has no run/activation provenance. Match it monotonically to
// existing root messages, preserving scoped execution evidence and disclosure IDs.
// Web optimism/replay requires a matching message ID. Anonymous saved rows may
// retain their snapshot-position identity, never a queued message's identity.
export function reconcileHistory(previous: Entry[], snapshot: Snapshot, announceNewUsers = false): Entry[] {
  const saved = fromHistory(snapshot);
  const roots = saved.filter((item) => item.kind !== "task" && item.kind !== "retained_tasks");
  const matched = new Set<number>();
  let searchAfter = 0;
  const matches = roots.map((entry) => {
    const index = previous.findIndex((item, index) => !matched.has(index) &&
      (entry.kind === "user" || index >= searchAfter) && same(entry, item));
    if (index >= 0) { matched.add(index); searchAfter = Math.max(searchAfter, index + 1); }
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
      : { ...entry, ...old, text: entry.text.length > old.text.length ? entry.text : old.text,
          result: old.result ?? entry.result, queued: false });
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
  if (run?.events) {
    let replay: Entry[] = [];
    for (const record of run.events) replay = appendEvent(replay, {
      ...record, run_id: record.run_id || run.id, ...(record.event === "user_message" ? { saved: true } : {}),
    });
    for (const record of pendingApprovals(run)) replay = appendEvent(replay, record);
    // Replace the active run's replayable records, rather than append chunks again.
    // Map equivalent scopes back to stable local IDs for expanded disclosures.
    const claimed = new Set<string>();
    replay = replay.map((entry, index) => {
      const scoped = entries.find((item) => !claimed.has(item.id) && item.runId === entry.runId && item.taskId === entry.taskId &&
        item.activation === entry.activation && item.kind === entry.kind && item.callId === entry.callId &&
        (entry.kind !== "user" || sameUserMessage(entry, item)));
      // Bootstrap history can already include the current run's user message or
      // committed root output before the subscription's active replay arrives.
      const old = scoped || (!entry.taskId ? entries.findLast((item) => !claimed.has(item.id) && !item.runId && same(entry, item)) : undefined);
      if (old) claimed.add(old.id);
      return { ...entry, id: old?.id || `run:${run.id}:${index}` };
    });
    const first = entries.findIndex((entry) => entry.runId === run.id || claimed.has(entry.id));
    const before = first < 0 ? entries : entries.slice(0, first);
    entries = [...before.filter((entry) => entry.runId !== run.id && !claimed.has(entry.id)), ...replay,
      ...(first < 0 ? [] : entries.slice(first).filter((entry) => entry.runId !== run.id && !claimed.has(entry.id)))];
  }
  // A first/explicit history load establishes a baseline, even if React renders
  // it after the selected session mounts. Only later snapshot additions announce
  // source activity; matching saved rows retain their existing activity counter.
  entries = reconcileHistory(entries, snapshot, announceUpdates);
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
      (entry.taskId || "") === (record.task_id || "") && (entry.activation || 0) === (record.activation || 0));
    entries = entries.map((entry, i) => i === index ? { ...entry, streaming: true } : entry);
  }
  return { ...current, entries, history: snapshot.history, activeRun: run };
}

export function applyFrame(current: LiveTranscript, frame: SessionFrame): LiveTranscript {
  if (current.position?.epoch === frame.epoch && (frame.cursor < current.position.cursor || (frame.type === "event" && frame.cursor === current.position.cursor))) return current;
  if (frame.type === "event" && current.position && current.position.epoch !== frame.epoch) return current;
  let next = current;
  if (frame.type === "snapshot") next = applySnapshot(current, frame.snapshot);
  else {
    const event = frame.record;
    let activeRun = current.activeRun;
    if (event.event === "run_started" && typeof event.run_id === "string") activeRun = { id: event.run_id, status: "running" };
    if (event.event === "approval" && !activeRun && typeof event.run_id === "string") activeRun = { id: event.run_id, status: "approval" };
    if (event.event === "run_finished" && event.run_id === activeRun?.id) activeRun = undefined;
    if (event.event === "approval" && activeRun) activeRun = { ...activeRun, pending_approvals: [...pendingApprovals(activeRun), event] };
    if (event.event === "approval_closed" && activeRun) activeRun = { ...activeRun, pending_approvals: pendingApprovals(activeRun).filter((item) => item.approval_id !== event.approval_id), events: undefined, approval: undefined };
    next = { ...current, activeRun, entries: appendEvent(current.entries, event) };
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
