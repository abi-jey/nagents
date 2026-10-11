import { definitions, events } from "./fixtures";
import type { ActivityState, AgentEvent, AgentId, AgentRun } from "./types";

export function initialState(): ActivityState {
  const agents: Record<string, AgentRun> = {};
  for (const definition of definitions) {
    agents[definition.id] = { definition, status: "running", activity: definition.role, startedAt: 0, records: [] };
  }
  return { now: 0, agents, seenEvents: [], nextMessageId: 1, followUps: {}, announcement: "Demo ready. All activity is simulated." };
}

export function applyEvent(state: ActivityState, event: AgentEvent): ActivityState {
  const existing = state.agents[event.agentId];
  if (!existing || state.seenEvents.includes(event.id)) return state;
  const agent = { ...existing, records: [...existing.records] };
  let announcement = state.announcement;
  if (event.type === "status") {
    agent.status = event.status;
    agent.activity = event.activity;
    agent.finishedAt = event.status === "completed" || event.status === "failed" ? event.at : undefined;
    if (event.status === "running") {
      agent.records = agent.records.map(record => record.kind === "tool" && record.tool.status === "waiting"
        ? { kind: "tool", tool: { ...record.tool, status: "running", startedAt: event.at, summary: "Running the approved command" } }
        : record);
    }
    announcement = `${agent.definition.name}: ${event.activity}`;
  } else if (event.type === "message") {
    agent.records.push({ kind: "message", message: { id: event.id, at: event.at, role: event.role, text: event.text } });
  } else if (event.type === "tool_start") {
    agent.records.push({ kind: "tool", tool: { ...event.tool, startedAt: event.at } });
    agent.activity = event.tool.summary;
  } else if (event.type === "tool_result") {
    agent.records = agent.records.map(record => record.kind === "tool" && record.tool.id === event.toolId
      ? { kind: "tool", tool: { ...record.tool, status: event.status, finishedAt: event.at, summary: event.summary, output: event.output } }
      : record);
    agent.activity = event.summary;
  } else if (event.type === "response_boundary") {
    const pending = agent.records.filter(record => record.kind === "message" && record.message.delivery === "queued");
    if (pending.length) {
      agent.records = agent.records.map(record => record.kind === "message" && record.message.delivery === "queued"
        ? { kind: "message", message: { ...record.message, delivery: "picked_up", pickedUpAt: event.at } }
        : record);
      agent.records.push({ kind: "message", message: { id: `${event.id}-ack`, at: event.at, role: "assistant", text: `I’ve picked up your ${pending.length === 1 ? "note" : `${pending.length} notes`} at this response boundary. I’ll include ${pending.length === 1 ? "it" : "them"} in this pass. (Simulated acknowledgement.)` } });
      announcement = `${agent.definition.name} picked up ${pending.length} queued ${pending.length === 1 ? "message" : "messages"}.`;
    }
  }
  return { ...state, agents: { ...state.agents, [event.agentId]: agent }, seenEvents: [...state.seenEvents, event.id], announcement };
}

export function queueMessage(state: ActivityState, agentId: AgentId, text: string): ActivityState {
  const agent = state.agents[agentId];
  const trimmed = text.trim();
  if (!agent || !trimmed) return state;
  const followUps = { ...state.followUps };
  const idle = agent.status === "completed" || agent.status === "failed";
  if (idle && followUps[agentId] === undefined) followUps[agentId] = state.now + 3;
  return {
    ...state, nextMessageId: state.nextMessageId + 1, followUps,
    announcement: `Message queued for ${agent.definition.name}. ${idle ? "A new demo response is scheduled." : "The current tool continues; the message will be picked up at the next response boundary."}`,
    agents: { ...state.agents, [agentId]: { ...agent, records: [...agent.records, { kind: "message", message: { id: `user-${state.nextMessageId}`, role: "user", text: trimmed, at: state.now, delivery: "queued" } }] } },
  };
}

export function advanceTo(state: ActivityState, at: number): ActivityState {
  if (at < state.now) return state;
  let next = { ...state, now: at };
  for (const event of events) if (event.at <= at) next = applyEvent(next, event);
  for (const [agentId, boundary] of Object.entries(next.followUps)) {
    if (boundary > at) continue;
    next = applyEvent(next, { id: `followup-${agentId}-${boundary}`, at: boundary, agentId, type: "response_boundary" });
    const followUps = { ...next.followUps };
    delete followUps[agentId];
    next = { ...next, followUps };
  }
  // A message can arrive after a final response boundary but before completion.
  // Keep it queued for a follow-up turn instead of stranding it on the old run.
  for (const [agentId, agent] of Object.entries(next.agents)) {
    if ((agent.status === "completed" || agent.status === "failed") && queuedCount(agent) > 0 && next.followUps[agentId] === undefined) {
      next = { ...next, followUps: { ...next.followUps, [agentId]: at + 3 } };
    }
  }
  return next;
}

export function nextEventTime(state: ActivityState): number {
  const next = events.find(event => event.at > state.now)?.at;
  const followUp = Math.min(...Object.values(state.followUps));
  return Math.min(next ?? Infinity, followUp) === Infinity ? state.now + 1 : Math.min(next ?? Infinity, followUp);
}

export function startDemo(): ActivityState { return advanceTo(initialState(), 8); }

export function queuedCount(agent: AgentRun): number {
  return agent.records.filter(record => record.kind === "message" && record.message.delivery === "queued").length;
}
