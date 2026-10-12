import { queuedCount, visibleAgents } from "./engine";
import type { ActivityState, AgentId, AgentRun } from "./types";

export interface TeamPartition {
  visible: AgentRun[];
  older: AgentRun[];
  recentCompletedIds: AgentId[];
  carriedAncestorIds: AgentId[];
}

/** Keep actionable work and its ancestry visible; archive only settled completions. */
export function partitionTeam(state: ActivityState, recentLimit = 3): TeamPartition {
  const agents = visibleAgents(state);
  const settled = agents.filter(agent => agent.status === "completed" && queuedCount(agent) === 0 && state.followUps[agent.definition.id] === undefined)
    .sort((a, b) => (b.finishedAt ?? b.startedAt) - (a.finishedAt ?? a.startedAt) || a.definition.id.localeCompare(b.definition.id));
  const limit = Number.isFinite(recentLimit) ? Math.max(0, Math.floor(recentLimit)) : 3;
  const recentCompletedIds = settled.slice(0, limit).map(agent => agent.definition.id);
  const settledIds = new Set(settled.map(agent => agent.definition.id));
  const visibleIds = new Set([...agents.filter(agent => !settledIds.has(agent.definition.id)).map(agent => agent.definition.id), ...recentCompletedIds]);
  const known = new Map(agents.map(agent => [agent.definition.id, agent]));
  const carriedAncestorIds: AgentId[] = [];
  for (const id of [...visibleIds]) {
    let parent = known.get(id)?.definition.parentId;
    const walked = new Set<AgentId>([id]);
    while (parent && known.has(parent) && !walked.has(parent)) {
      walked.add(parent);
      if (!visibleIds.has(parent)) { visibleIds.add(parent); carriedAncestorIds.push(parent); }
      parent = known.get(parent)?.definition.parentId;
    }
  }
  return {
    visible: agents.filter(agent => visibleIds.has(agent.definition.id)),
    older: settled.filter(agent => !visibleIds.has(agent.definition.id)),
    recentCompletedIds,
    carriedAncestorIds,
  };
}

export interface TeamViewportMeasurements {
  /** Space left for delegated agents; the main agent is measured separately. */
  availableHeight: number;
  rowHeight: number;
  originHeight: number;
  moreHeight: number;
  maxRecent?: number;
}

export interface FittedTeamPartition extends TeamPartition {
  selectedRecentLimit: number;
  /** The actionable rows and their More control need scrolling. */
  activeOverflow: boolean;
  estimatedHeight: number;
}

function measuredPixels(value: number): number {
  return Number.isFinite(value) ? Math.min(Number.MAX_SAFE_INTEGER, Math.max(0, value)) : 0;
}

/** Fit recent completions around actionable work, never hiding an active branch. */
export function fitTeamToViewport(state: ActivityState, measurements: TeamViewportMeasurements): FittedTeamPartition {
  const availableHeight = measuredPixels(measurements.availableHeight);
  const rowHeight = measuredPixels(measurements.rowHeight);
  const originHeight = measuredPixels(measurements.originHeight);
  const moreHeight = measuredPixels(measurements.moreHeight);
  const requestedRecent = measurements.maxRecent ?? 3;
  const maxRecent = Number.isFinite(requestedRecent) ? Math.min(3, Math.max(0, Math.floor(requestedRecent))) : 3;
  for (let limit = maxRecent; limit >= 0; limit -= 1) {
    const partition = partitionTeam(state, limit);
    const origins = new Set(partition.visible.map(agent => agent.definition.originMessageId));
    const estimatedHeight = Math.min(Number.MAX_SAFE_INTEGER,
      partition.visible.length * rowHeight + origins.size * originHeight + (partition.older.length > 0 ? moreHeight : 0));
    if (estimatedHeight <= availableHeight || limit === 0) {
      return {
        ...partition,
        selectedRecentLimit: partition.recentCompletedIds.length,
        activeOverflow: limit === 0 && partition.visible.length > 0 && estimatedHeight > availableHeight,
        estimatedHeight,
      };
    }
  }
  // The bounded loop always visits zero, which preserves every actionable row.
  throw new Error("No team viewport partition was selected");
}

export interface SpawnGroup {
  id: string;
  at: number;
  originMessageId: string;
  parentId: AgentId;
  agentIds: AgentId[];
}

/** Derive creation markers only from agents that have actually appeared. */
export function spawnGroups(state: ActivityState): SpawnGroup[] {
  const groups = new Map<string, SpawnGroup>();
  for (const { definition } of visibleAgents(state)) {
    const { spawnedAt: at, originMessageId, parentId, id: agentId } = definition;
    const id = JSON.stringify([parentId, originMessageId, at]);
    const group = groups.get(id);
    if (group) group.agentIds.push(agentId);
    else groups.set(id, { id, at, originMessageId, parentId, agentIds: [agentId] });
  }
  return [...groups.values()].sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));
}
