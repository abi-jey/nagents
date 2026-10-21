import type { ContextStats, RetainedTask, Snapshot, WireEvent } from "../../types.js";
import { validContextStats } from "../../api/context.js";
import type { SessionAgentSelection } from "../sessions/sessionAgent.js";
import type { RemoteAuthentication } from "./RemoteStatus.js";

export const MAIN_AGENT = "";
export function providerLabel(agent: Pick<TeamAgent, "provider" | "providerKind">): string {
  if (agent.providerKind === "a2a") return "Remote A2A";
  return agent.provider || agent.providerKind;
}
export type AgentPhase = "running" | "waiting" | "done" | "failed" | "cancelled" | "ready";
export type TeamAgent = {
  id: string; name: string; profile: string; parentId: string; prompt: string; phase: AgentPhase; reason: string;
  provider: string; providerKind: string; model: string; startedAt: number; updatedAt: number;
  reasoningEffort?: string;
  context?: ContextStats; compactTrigger?: string; compactTokens?: number; compactThreshold?: number; compactMessages?: number;
  canMessage: boolean; messageReason: string; task?: RetainedTask;
  authentication?: RemoteAuthentication; trustRemoteTools?: boolean;
};
export type TaskActivity = WireEvent & {
  event: "task_activity"; task_id: string; activation: number; sequence: number;
  timestamp: number; payload: { type: string; [key: string]: unknown };
};
export type TaskReceipt = {
  message_id: string; task_id: string; text?: string;
  status: "queued" | "picked_up" | "cancelled" | "error";
  revision: number; reason?: string; queued_at?: string; picked_up_at?: string;
};
export type TaskDetail = {
  task: RetainedTask; history: Snapshot["history"]; activity?: TaskActivity[];
  receipts?: TaskReceipt[]; context?: ContextStats; truncated?: boolean;
  activity_cursor?: number;
  activity_truncated?: boolean;
  history_truncated?: boolean;
  before?: string;
};
export type TeamProfile = { id: string; name: string; description: string; remote_name?: string; remote_description?: string; metadata_status?: string; provider: string; model: string; source?: string; used: boolean; available: boolean; can_start?: boolean; unavailable_reason?: string; authentication?: RemoteAuthentication; trust_remote_tools?: boolean };
export type TeamSubagentUsage = {
  limits: { max_concurrent_subagents: number; max_subagent_executions: number; max_subagent_depth: number };
  used_executions: number;
  remaining_executions: number;
  occupied_slots: number;
  available_slots: number;
  retained_tasks: number;
  max_retained_tasks: number;
  depth: number;
  reset: string;
};

/** Definitions outlive individual conversations; their names are not task display names. */
export function definitionUsage(profile: TeamProfile, agents: readonly TeamAgent[]): string {
  const conversations = agents.filter(agent => agent.profile === profile.id);
  const active = conversations.filter(agent => agent.phase === "running" || agent.phase === "waiting").length;
  if (active) return `${active} active`;
  if (conversations.length) return `${conversations.length} conversation${conversations.length === 1 ? "" : "s"}`;
  return profile.used ? "Used in chat" : "Not started";
}

export function conversationAgent(selection: SessionAgentSelection | undefined, context: ContextStats | undefined,
  running: boolean, approval: boolean): TeamAgent {
  const identity = selection?.active || selection?.effective;
  return { id: MAIN_AGENT, parentId: MAIN_AGENT, name: identity?.agent || "Assistant", profile: identity?.agent || "", prompt: "",
    reason: approval ? "Waiting for approval" : "", phase: approval ? "waiting" : running ? "running" : "ready",
    provider: identity?.provider || "", providerKind: identity?.provider_kind || "", model: identity?.model || "",
    reasoningEffort: identity?.reasoning_effort,
    context, compactTrigger: context?.compaction?.trigger, compactTokens: context?.compaction?.token_budget,
    compactThreshold: context?.compaction?.token_threshold, compactMessages: context?.compaction?.message_threshold,
    startedAt: 0, updatedAt: 0, canMessage: true, messageReason: "" };
}

export function taskAgent(task: RetainedTask): TeamAgent {
  const context = validContextStats(task.context) ? task.context : undefined;
  const compaction = context?.compaction || task.compaction;
  const waiting = task.phase?.startsWith("waiting");
  const phase: AgentPhase = task.status === "failed" ? "failed" : task.status === "cancelled" ? "cancelled"
    : task.status === "completed" ? "done" : waiting ? "waiting" : "running";
  const reason = task.phase_detail || (task.phase === "waiting_approval" ? "Waiting for approval"
    : task.phase === "waiting_children" || task.phase === "waiting_delegation" ? "Waiting for delegated work"
    : task.phase === "waiting_input" ? "Waiting for a message" : task.phase === "stopping" ? "Stopping"
    : phase === "waiting" ? "Waiting for delegated work" : phase === "failed" ? task.error : phase === "done" ? task.result : task.prompt);
  return { id: task.id, name: task.name, profile: task.profile || "", parentId: task.parent_task_id || MAIN_AGENT, prompt: task.prompt,
    phase, reason, provider: task.provider || "", providerKind: task.provider_kind || task.provider || "", model: task.model || "",
    reasoningEffort: task.reasoning_effort,
    startedAt: task.started_at || 0, updatedAt: task.updated_at || task.completed_at || task.started_at || 0,
    context, compactTrigger: compaction?.trigger || task.compact_trigger, compactTokens: compaction?.token_budget, compactThreshold: compaction?.token_threshold, compactMessages: compaction?.message_threshold,
    canMessage: task.can_message === true, messageReason: task.unavailable_reason || task.message_unavailable_reason || "", task };
}

/** Newest spawns first; status and descendant activity never reorder a branch. */
export function orderedTeam(agents: readonly TeamAgent[]): { children: Map<string, TeamAgent[]> } {
  const known = new Map(agents.map(agent => [agent.id, agent]));
  const children = new Map<string, TeamAgent[]>();
  for (const agent of agents) {
    if (!agent.id) continue;
    let parent = agent.parentId;
    const visited = new Set([agent.id]);
    for (let cursor = parent; cursor; cursor = known.get(cursor)?.parentId || "") {
      if (visited.has(cursor) || !known.has(cursor)) { parent = MAIN_AGENT; break; }
      visited.add(cursor);
    }
    const group = children.get(parent) || [];
    group.push(agent); children.set(parent, group);
  }
  for (const group of children.values()) group.sort((a, b) => b.startedAt - a.startedAt || a.id.localeCompare(b.id));
  return { children };
}

export function agentAncestors(id: string, agents: readonly TeamAgent[]): string[] {
  const known = new Map(agents.map(agent => [agent.id, agent]));
  const result: string[] = [], visited = new Set([id]);
  let parent = known.get(id)?.parentId;
  while (parent !== undefined && !visited.has(parent) && known.has(parent)) {
    result.push(parent); visited.add(parent); parent = known.get(parent)?.parentId;
  }
  return result;
}

export function phaseLabel(agent: Pick<TeamAgent, "phase" | "reason">): string {
  if (agent.phase === "waiting") return agent.reason || "Waiting";
  return { running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled", ready: "Ready" }[agent.phase];
}

export function validTaskActivity(event: WireEvent): event is TaskActivity {
  return event.event === "task_activity" && typeof event.task_id === "string" && !!event.task_id &&
    typeof event.activation === "number" && Number.isSafeInteger(event.activation) &&
    typeof event.sequence === "number" && Number.isSafeInteger(event.sequence) &&
    !!event.payload && typeof event.payload === "object" && !Array.isArray(event.payload) &&
    "type" in event.payload && typeof event.payload.type === "string";
}

/** Identity is task + activation + host sequence, never a provider tool id. */
export function mergeTaskActivity(previous: readonly TaskActivity[], incoming: readonly TaskActivity[]): TaskActivity[] {
  const unique = new Map(previous.map(event => [`${event.task_id}:${event.activation}:${event.sequence}`, event]));
  for (const event of incoming) if (validTaskActivity(event)) unique.set(`${event.task_id}:${event.activation}:${event.sequence}`, event);
  return [...unique.values()].sort((a, b) => a.activation - b.activation || a.sequence - b.sequence).slice(-600);
}

export class TaskDrafts {
  private drafts = new Map<string, { text: string; revision: number }>();
  private attempts = new Map<string, { message_id: string; text: string }>();
  private attemptRevisions = new Map<string, number>();
  private key(root: string, task: string) { return JSON.stringify([root, task]); }
  get(root: string, task: string) { return this.drafts.get(this.key(root, task))?.text || ""; }
  set(root: string, task: string, text: string) { const key = this.key(root, task); this.drafts.set(key, { text, revision: (this.drafts.get(key)?.revision || 0) + 1 }); }
  prepare(root: string, task: string, text: string, id: () => string) {
    const key = this.key(root, task), previous = this.attempts.get(key);
    if (previous?.text === text) return previous;
    const value = { message_id: id(), text }; this.attempts.set(key, value); this.attemptRevisions.set(value.message_id, this.drafts.get(key)?.revision || 0); return value;
  }
  confirmed(root: string, task: string, message: { message_id: string; text: string }) {
    const key = this.key(root, task);
    if (this.attempts.get(key)?.message_id === message.message_id) this.attempts.delete(key);
    const draft = this.drafts.get(key);
    if (draft?.text === message.text && draft.revision === this.attemptRevisions.get(message.message_id)) this.set(root, task, "");
    this.attemptRevisions.delete(message.message_id);
  }
  rejected(root: string, task: string, messageId: string) {
    const key = this.key(root, task);
    if (this.attempts.get(key)?.message_id === messageId) this.attempts.delete(key);
  }
}
