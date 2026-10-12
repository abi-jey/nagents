import { definitions, events, mainMessages } from "./fixtures";
import type { ActivityState, AgentEvent, AgentId, AgentRun } from "./types";

export function initialState(): ActivityState {
  const agents: Record<string, AgentRun> = {};
  for (const definition of definitions) {
    agents[definition.id] = { definition, status: "running", activity: definition.role, startedAt: definition.spawnedAt, records: [] };
  }
  const initialMessages = mainMessages.filter(message => message.at === 0);
  return { now: 0, agents, mainMessages: initialMessages, pendingMainReplies: [], seenEvents: initialMessages.map(message => message.id), nextMessageId: 1, followUps: {}, announcement: "Demo ready. All activity is simulated." };
}

export function applyEvent(state: ActivityState, event: AgentEvent): ActivityState {
  const existing = state.agents[event.agentId];
  if (!existing || event.at < existing.definition.spawnedAt || state.seenEvents.includes(event.id)) return state;
  if (event.type === "return") {
    // A nested child reports inside its parent's thread, not into the main chat.
    // Only a completed result can be surfaced, and replay uses the event identity.
    if (existing.status !== "completed") return state;
    const message = { id: event.id, at: event.at, role: "assistant" as const, text: event.text, returnedAgentId: existing.definition.id, originMessageId: existing.definition.originMessageId };
    const parent = state.agents[existing.definition.parentId];
    if (existing.definition.parentId !== "assistant" && !parent) return state;
    return {
      ...state,
      agents: parent ? { ...state.agents, [parent.definition.id]: { ...parent, records: [...parent.records, { kind: "message", message }] } } : state.agents,
      mainMessages: parent ? state.mainMessages : [...state.mainMessages, message],
      seenEvents: [...state.seenEvents, event.id],
      announcement: `${existing.definition.name} returned a result to ${parent?.definition.name ?? "Assistant"}.`,
    };
  }
  const agent = { ...existing, records: [...existing.records] };
  let followUps = state.followUps;
  if (event.type === "response_boundary" || event.type === "tool_start" || (event.type === "status" && (event.status === "running" || event.status === "waiting"))) {
    // An idle timer cannot create a response boundary inside a resumed run.
    if (followUps[event.agentId] !== undefined) {
      followUps = { ...followUps };
      delete followUps[event.agentId];
    }
  }
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
      agent.records.push({ kind: "message", message: { id: `${event.id}-ack`, at: event.at, role: "assistant", text: `Picked up your ${pending.length === 1 ? "note" : `${pending.length} notes`}. (Simulated.)` } });
      announcement = `${agent.definition.name} picked up ${pending.length} queued ${pending.length === 1 ? "message" : "messages"}.`;
    }
  }
  return { ...state, agents: { ...state.agents, [event.agentId]: agent }, followUps, seenEvents: [...state.seenEvents, event.id], announcement };
}

function idleForFollowUp(agent: AgentRun): boolean {
  return (agent.status === "completed" || agent.status === "failed")
    && !agent.records.some(record => record.kind === "tool" && (record.tool.status === "running" || record.tool.status === "waiting"));
}

export function queueMessage(state: ActivityState, agentId: AgentId, text: string): ActivityState {
  const agent = state.agents[agentId];
  const trimmed = text.trim();
  if (!agent || agent.definition.spawnedAt > state.now || !trimmed) return state;
  const followUps = { ...state.followUps };
  const idle = idleForFollowUp(agent);
  if (idle && followUps[agentId] === undefined) followUps[agentId] = state.now + 3;
  return {
    ...state, nextMessageId: state.nextMessageId + 1, followUps,
    announcement: `Message queued for ${agent.definition.name}. ${idle ? "A new demo response is scheduled." : "The current tool continues; the message will be picked up with the next response."}`,
    agents: { ...state.agents, [agentId]: { ...agent, records: [...agent.records, { kind: "message", message: { id: `user-${state.nextMessageId}`, role: "user", text: trimmed, at: state.now, delivery: "queued" } }] } },
  };
}

export function queueMainMessage(state: ActivityState, text: string): ActivityState {
  const trimmed = text.trim();
  if (!trimmed) return state;
  const id = `main-user-${state.nextMessageId}`;
  return {
    ...state,
    nextMessageId: state.nextMessageId + 1,
    mainMessages: [...state.mainMessages, { id, at: state.now, role: "user", text: trimmed }],
    pendingMainReplies: [...state.pendingMainReplies, { id: `${id}-reply`, at: state.now + 1, originMessageId: id }],
    announcement: "Message sent to the main assistant. A simulated response is scheduled; child tasks continue.",
  };
}

function scheduleIdleFollowUps(state: ActivityState): ActivityState {
  let next = state;
  for (const [agentId, agent] of Object.entries(state.agents)) {
    if (idleForFollowUp(agent) && queuedCount(agent) > 0 && next.followUps[agentId] === undefined) {
      next = { ...next, followUps: { ...next.followUps, [agentId]: state.now + 3 } };
    }
  }
  return next;
}

export function advanceTo(state: ActivityState, at: number): ActivityState {
  if (at < state.now) return state;
  let next = state;
  // Process all lanes in timestamp order, including interactive replies. A jump
  // to the end must produce the same transcript as stepping the clock normally.
  for (;;) {
    const due = Math.min(
      ...events.filter(event => !next.seenEvents.includes(event.id)).map(event => event.at),
      ...mainMessages.filter(message => !next.seenEvents.includes(message.id)).map(message => message.at),
      ...Object.values(next.followUps),
      ...next.pendingMainReplies.map(reply => reply.at),
    );
    if (due > at || due === Infinity) break;
    next = { ...next, now: due };
    for (const message of mainMessages) {
      if (message.at !== due || next.seenEvents.includes(message.id)) continue;
      next = { ...next, mainMessages: [...next.mainMessages, message], seenEvents: [...next.seenEvents, message.id] };
    }
    for (const event of events) if (event.at === due) next = applyEvent(next, event);
    for (const [agentId, boundary] of Object.entries(next.followUps)) {
      if (boundary !== due) continue;
      const agent = next.agents[agentId];
      if (agent && idleForFollowUp(agent) && queuedCount(agent) > 0) {
        next = applyEvent(next, { id: `followup-${agentId}-${boundary}`, at: boundary, agentId, type: "response_boundary" });
      }
      const followUps = { ...next.followUps };
      delete followUps[agentId];
      next = { ...next, followUps };
    }
    for (const reply of next.pendingMainReplies.filter(reply => reply.at === due)) {
      next = {
        ...next,
        mainMessages: [...next.mainMessages, { ...reply, role: "assistant", text: "I’ve received your message here. (Simulated response.)" }],
        seenEvents: [...next.seenEvents, reply.id],
        pendingMainReplies: next.pendingMainReplies.filter(pending => pending.id !== reply.id),
        announcement: "The main assistant replied. Child tasks and their message queues were unchanged.",
      };
    }
    // A note arriving after the final response boundary belongs to a new turn.
    next = scheduleIdleFollowUps(next);
  }
  return { ...next, now: at };
}

export function nextEventTime(state: ActivityState): number {
  const next = Math.min(
    ...events.filter(event => event.at > state.now).map(event => event.at),
    ...mainMessages.filter(message => message.at > state.now).map(message => message.at),
    ...definitions.filter(definition => definition.spawnedAt > state.now).map(definition => definition.spawnedAt),
    ...Object.values(state.followUps),
    ...state.pendingMainReplies.map(reply => reply.at),
  );
  return next === Infinity ? state.now + 1 : next;
}

export function startDemo(): ActivityState { return initialState(); }

export function visibleAgents(state: ActivityState): AgentRun[] {
  return Object.values(state.agents).filter(agent => agent.definition.spawnedAt <= state.now);
}

export function queuedCount(agent: AgentRun): number {
  return agent.records.filter(record => record.kind === "message" && record.message.delivery === "queued").length;
}
