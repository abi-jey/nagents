import { appendEvent, type Entry } from "./transcript.js";
import type { AgentPhase, TaskActivity, TeamAgent } from "../team/model.js";

export type DelegationPresentation = {
  kind: "start" | "result"; taskId: string; name: string; profile: string;
  phase: AgentPhase; prompt: string; text: string; latest?: Entry; lastTool?: Entry; sources: Entry[];
};
export type DelegationOptions = {
  agents: readonly TeamAgent[]; activity: readonly TaskActivity[]; parentId?: string;
  inspect: (id: string) => void;
};

function argumentsOf(entry: Entry): Record<string, unknown> {
  try { const value: unknown = JSON.parse(entry.inputs || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  } catch { return {}; }
}

function savedTasks(entry: Entry): string[] {
  if (entry.kind !== "context" || !entry.text.startsWith("BACKGROUND TASK NOTIFICATION:")) return [];
  try {
    const envelope: unknown = JSON.parse(entry.text.slice(entry.text.indexOf("\n") + 1));
    if (!envelope || typeof envelope !== "object" || !("tasks" in envelope) || !Array.isArray(envelope.tasks) ||
      !("human_messages" in envelope) || !Array.isArray(envelope.human_messages) || envelope.human_messages.length || "wakeups" in envelope) return [];
    const ids: string[] = [];
    for (const task of envelope.tasks) {
      if (!task || typeof task !== "object" || typeof task.task_id !== "string" || !task.task_id ||
        (task.activation !== undefined && task.activation !== 0) || (task.followup !== undefined && task.followup !== 0) ||
        (task.trigger !== undefined && task.trigger !== "delegation")) return [];
      ids.push(task.task_id);
    }
    return ids;
  } catch { return []; }
}

function ownActivity(events: readonly TaskActivity[], taskId: string, activation: number): Entry[] {
  let entries: Entry[] = [];
  for (const event of events) {
    if (event.task_id !== taskId || event.activation !== activation ||
      !["response_metadata", "text_chunk", "text_done", "tool_call_progress", "tool_call", "tool_execution_started", "tool_output", "tool_result", "error", "approval", "approval_closed"].includes(event.payload.type)) continue;
    entries = appendEvent(entries, { ...event.payload, event: event.payload.type,
      task_id: "", parent_task_id: "", run_id: "", activation, followup: 0 });
  }
  return entries;
}

function terminal(entry: Entry | undefined): boolean {
  return !!entry && (entry.result !== undefined || ["Completed", "Error", "Cancelled"].includes(entry.state || "") ||
    !!entry.recorded && ["completed", "failed", "cancelled"].includes(entry.recordedStatus || ""));
}

/** Presentation only. Execution, approvals, model inputs and source records remain unchanged. */
export class DelegationFlow {
  private finished = new Map<string, { anchor: string; phase: AgentPhase; text: string }>();
  private visibleEvidence = new Set<string>();
  private activitySource?: readonly TaskActivity[];
  private activityCache = new Map<string, Entry[]>();

  project(entries: Entry[], options: DelegationOptions): Entry[] {
    if (!entries.length) { this.finished.clear(); this.visibleEvidence.clear(); return entries; }
    if (this.activitySource !== options.activity) {
      this.activitySource = options.activity; this.activityCache.clear();
    }
    const parentId = options.parentId || "";
    const direct = new Map(options.agents.filter(agent => agent.id && agent.parentId === parentId).map(agent => [agent.id, agent]));
    const covered = new Set<string>();
    const rows: { entry: Entry; position: number }[] = [];
    const positions = new Map(entries.map((entry, index) => [entry.id, index]));
    const completionNotices = new Map<string, Entry>();
    for (const entry of entries) if (entry.kind === "notification" && entry.cause === "completion" && entry.sourceTaskId &&
      (!(entry.taskId || "") || entry.taskId === parentId) && !completionNotices.has(entry.sourceTaskId)) completionNotices.set(entry.sourceTaskId, entry);
    const contexts = new Map(entries.map(entry => [entry.id, savedTasks(entry)]));
    const calls = entries.filter(entry => entry.kind === "tool" && (entry.title === "delegate" || entry.delegatedTaskId) &&
      !entry.remoteReview && (!(entry.taskId || "") || entry.taskId === parentId) && !("remote_operation" in argumentsOf(entry)));
    for (const call of calls) {
      let savedId = "";
      try {
        const acknowledgement: unknown = JSON.parse(call.result || "{}");
        if (acknowledgement && typeof acknowledgement === "object" && "task_id" in acknowledgement && typeof acknowledgement.task_id === "string") savedId = acknowledgement.task_id;
      } catch { /* An incomplete or non-JSON acknowledgement carries no task identity. */ }
      const candidate = call.delegatedTaskId || savedId;
      // Saved JSON supplies a correlation hint, never execution authority.
      const taskId = call.delegatedTaskId || (direct.has(candidate) || entries.some(entry => entry.kind === "task" && entry.taskId === candidate) ? candidate : "");
      const agent = direct.get(taskId);
      // A known grandchild belongs in its own parent's conversation.
      if (taskId && options.agents.some(candidate => candidate.id === taskId) && !agent) continue;
      const evidence = entries.filter(entry => entry.kind === "task" && entry.taskId === taskId && (entry.activation || 0) === 0);
      const lifecycle = evidence.find(entry => !entry.recorded) || evidence[0];
      const activation = lifecycle?.activation || 0;
      const activityKey = `${taskId}:${activation}`;
      let local = this.activityCache.get(activityKey);
      if (!local) { local = ownActivity(options.activity, taskId, activation); this.activityCache.set(activityKey, local); }
      const latest = local.findLast(entry => entry.kind === "tool" || entry.kind === "assistant" && !!(entry.text || entry.responseMetadata?.summary));
      const args = argumentsOf(call);
      const phase: AgentPhase = call.error ? "failed" : terminal(lifecycle)
        ? lifecycle?.error || lifecycle?.state === "Error" || lifecycle?.recordedStatus === "failed" ? "failed" : lifecycle?.state === "Cancelled" || lifecycle?.recordedStatus === "cancelled" ? "cancelled" : "done"
        : ["Waiting for approval", "Approval required"].includes(call.state || "") ? "waiting" : agent?.phase || "running";
      const name = agent?.name || lifecycle?.title || (taskId ? taskId : "");
      const profile = agent?.profile || (typeof args.agent === "string" ? args.agent : "Agent");
      const prompt = typeof args.prompt === "string" ? args.prompt : agent?.prompt || lifecycle?.inputs || "";
      const notice = completionNotices.get(taskId);
      const relatedContexts = entries.filter(entry => contexts.get(entry.id)?.includes(taskId));
      const sources = [call, ...evidence, ...relatedContexts, ...(notice ? [notice] : [])];
      const key = `${taskId}:${activation}`;
      if (taskId && ["done", "failed", "cancelled"].includes(phase) && !this.finished.has(key)) {
        const source = notice || relatedContexts[0];
        const anchor = lifecycle?.completionAfterId || (source ? entries[Math.max(0, positions.get(source.id)! - 1)]?.id : entries.at(-1)?.id) || call.id;
        this.finished.set(key, { anchor, phase, text: lifecycle?.error || lifecycle?.result || notice?.text || agent?.task?.error || agent?.task?.result || agent?.reason || "" });
      }
      const finished = this.finished.get(key);
      // Checkpoints can rebind the anchor's local ID. Follow the canonical
      // lifecycle reference instead of falling back to the end of the chat.
      if (finished && lifecycle?.completionAfterId && positions.has(lifecycle.completionAfterId))
        finished.anchor = lifecycle.completionAfterId;
      const presentation: DelegationPresentation = { kind: "start", taskId, name, profile, phase: finished?.phase || phase,
        prompt, text: call.error || finished?.text || "", latest, lastTool: local.findLast(entry => entry.kind === "tool"), sources };
      rows.push({ entry: { ...call, kind: "status", delegation: presentation, taskId: undefined,
        activity: Math.max(call.activity || 0, lifecycle?.activity || 0), title: name || profile }, position: positions.get(call.id)! });
      if (taskId) covered.add(taskId);
      if (finished) rows.push({ entry: { id: `delegation-result:${key}`, kind: "status", text: finished.text,
        title: name || profile, state: finished.phase === "failed" ? "Failed" : finished.phase,
        error: finished.phase === "failed" ? finished.text || "The subagent failed." : undefined,
        runId: call.runId, activity: lifecycle?.activity || notice?.activity,
        delegation: { ...presentation, kind: "result", text: finished.text } },
        position: (positions.get(finished.anchor) ?? entries.length - 1) + .25 });
    }
    const originals = entries.flatMap((entry, index) => {
      if (calls.includes(entry) || entry.kind === "retained_tasks" || entry.taskId) return [];
      if (entry.kind === "notification" && entry.cause === "completion" && entry.sourceTaskId &&
        completionNotices.get(entry.sourceTaskId) === entry && covered.has(entry.sourceTaskId) && !this.visibleEvidence.has(entry.id)) return [];
      const ids = contexts.get(entry.id) || [];
      if (ids.length && ids.every(id => covered.has(id)) && !this.visibleEvidence.has(entry.id)) return [];
      if (entry.kind === "context" || entry.kind === "notification") this.visibleEvidence.add(entry.id);
      return [{ entry, position: index }];
    });
    return [...originals, ...rows].sort((a, b) => a.position - b.position).map(item => item.entry);
  }
}

export function delegationPreview(item: DelegationPresentation): { tool: string; text: string; live: boolean } {
  if (item.kind === "result") return { tool: "", text: item.text, live: false };
  if (item.latest?.kind === "tool") {
    const raw = item.latest.inputs || "";
    let text = raw;
    try { text = JSON.stringify(JSON.parse(raw)); } catch { /* Partial arguments remain inspectable text. */ }
    return { tool: item.latest.title || "", text, live: item.latest.toolProgress === "streaming" };
  }
  return { tool: "", text: item.latest?.text || item.latest?.responseMetadata?.summary || item.text || item.prompt,
    live: item.phase === "running" || item.phase === "waiting" };
}
