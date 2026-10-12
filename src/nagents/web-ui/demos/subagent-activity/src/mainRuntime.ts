import { resolveAgentRuntime, resolveRuntimeSettings } from "./agentRuntime";
import type { AgentRuntime, RuntimeSettingsMap } from "./agentRuntime";
import { queuedCount, visibleAgents } from "./engine";
import { MAIN_AGENT_DEFINITION } from "./fixtures";
import type { ActivityState, AgentRun } from "./types";

export const MAIN_AGENT_ID = MAIN_AGENT_DEFINITION.id;

/** Read-only adapter for the same status/settings UI; never added to child execution. */
export function mainAgentSnapshot(state: ActivityState, acceptedSettings: RuntimeSettingsMap): AgentRun {
  const settings = resolveRuntimeSettings(MAIN_AGENT_DEFINITION, acceptedSettings[MAIN_AGENT_ID]);
  const pendingIds = new Set(state.pendingMainReplies.map(reply => reply.originMessageId));
  const working = visibleAgents(state).filter(agent => agent.status === "running" || agent.status === "waiting" || queuedCount(agent) > 0);
  const attention = visibleAgents(state).filter(agent => agent.status === "failed");
  const activity = pendingIds.size ? "Reply queued"
    : working.length ? `Coordinating ${working.length} ${working.length === 1 ? "agent" : "agents"}`
      : attention.length ? `${attention.length} ${attention.length === 1 ? "agent needs" : "agents need"} attention` : "Ready";
  return {
    definition: { ...MAIN_AGENT_DEFINITION, provider: settings.providerLabel, model: settings.model },
    startedAt: 0,
    status: pendingIds.size || working.length ? "running" : attention.length ? "waiting" : "completed",
    activity,
    records: state.mainMessages.map(message => ({ kind: "message", message: pendingIds.has(message.id) ? { ...message, delivery: "queued" } : { ...message } })),
  };
}

export function resolveMainRuntime(state: ActivityState, acceptedSettings: RuntimeSettingsMap): AgentRuntime {
  const runtime = resolveAgentRuntime(mainAgentSnapshot(state, acceptedSettings), state, acceptedSettings);
  return {
    ...runtime,
    estimateDetail: "Demo estimate: 18,000 fixture prompt tokens plus main instructions and accepted conversation text (UTF-8 bytes ÷ 4). Inputs waiting for the next main response are excluded. Child transcripts are counted only when their returned summary reaches this conversation.",
  };
}
