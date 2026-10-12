import { queuedCount, visibleAgents } from "./engine";
import { MAIN_AGENT_ID } from "./mainRuntime";
import type { ActivityState, AgentRun } from "./types";

export interface AgentLifecycle {
  state: "running" | "waiting" | "done" | "failed";
  label: "Running" | "Waiting" | "Done" | "Failed";
  detail: string;
  originLabel: string;
  originMessageId: string;
  parentName: string;
  /** Conversation start for the main agent; creation time for a child. */
  startedAt: number;
  queuedMessages: number;
}

/** Presentation of observed activity only; it does not change execution or queues. */
export function resolveAgentLifecycle(agent: AgentRun, state: ActivityState): AgentLifecycle {
  const isMain = agent.definition.id === MAIN_AGENT_ID;
  const parentName = isMain ? "" : agent.definition.parentId === MAIN_AGENT_ID
    ? "Assistant" : state.agents[agent.definition.parentId]?.definition.name ?? agent.definition.parentId;
  const origin = {
    originLabel: isMain ? "Conversation started" : `Spawned by ${parentName}`,
    originMessageId: agent.definition.originMessageId,
    parentName,
    startedAt: isMain ? agent.startedAt : agent.definition.spawnedAt,
    queuedMessages: isMain ? state.pendingMainReplies.length : queuedCount(agent),
  };
  const result = (status: AgentLifecycle["state"], label: AgentLifecycle["label"], detail: string): AgentLifecycle => ({ ...origin, state: status, label, detail });

  if (isMain) {
    const answered = new Set(state.mainMessages.filter(message => message.role === "assistant" && !message.returnedAgentId).map(message => message.originMessageId));
    const unanswered = state.mainMessages.some(message => message.role === "user" && !answered.has(message.id));
    if (state.pendingMainReplies.length || unanswered) return result("running", "Running", "Responding to your message");
    // Nested agents report to their parent, not directly to the main assistant.
    const children = visibleAgents(state).filter(child => child.definition.parentId === MAIN_AGENT_ID);
    const pending = children.filter(child => child.status === "running" || child.status === "waiting" || queuedCount(child) > 0 || state.followUps[child.definition.id] !== undefined);
    const failed = children.filter(child => child.status === "failed" && !pending.includes(child));
    if (pending.length) {
      const attention = failed.length ? ` · ${failed.map(child => child.definition.name).join(", ")} ${failed.length === 1 ? "needs" : "need"} attention` : "";
      return result("waiting", "Waiting", `Results from ${pending.map(child => child.definition.name).join(", ")}${attention}`);
    }
    if (failed.length) return result("waiting", "Waiting", `${failed.map(child => child.definition.name).join(", ")} ${failed.length === 1 ? "needs" : "need"} attention`);
    return result("waiting", "Waiting", "Your next message");
  }

  const tools = agent.records.flatMap(record => record.kind === "tool" ? [record.tool] : []);
  const runningTool = tools.findLast(tool => tool.status === "running");
  if (runningTool) return result("running", "Running", runningTool.summary || runningTool.title);
  const waitingTool = tools.findLast(tool => tool.status === "waiting");
  if (waitingTool) {
    const detail = /approval/i.test(waitingTool.summary) ? `Approval · ${waitingTool.title}` : waitingTool.summary || waitingTool.title;
    return result("waiting", "Waiting", detail);
  }
  const settled = agent.status === "completed" || agent.status === "failed";
  if (settled && (origin.queuedMessages > 0 || state.followUps[agent.definition.id] !== undefined)) return result("waiting", "Waiting", "Next response");
  if (agent.status === "completed") return result("done", "Done", agent.activity);
  if (agent.status === "failed") return result("failed", "Failed", agent.activity);
  if (agent.status === "waiting") return result("waiting", "Waiting", agent.activity);
  // A finished tool is evidence, not the agent's current activity. Preserve the
  // running state without presenting the previous tool result as ongoing work.
  return result("running", "Running", agent.definition.role);
}
