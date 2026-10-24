import type { TeamAgent } from "./model.js";

export type TeamBucket = "running" | "past";
export type DefinitionGroup = {
  key: string; parentId: string; profile: string;
  running: TeamAgent[]; past: TeamAgent[];
};

export function teamBucket(agent: TeamAgent): TeamBucket {
  return agent.phase === "running" || agent.phase === "waiting" ? "running" : "past";
}
export function definitionGroupKey(parentId: string, profile: string): string {
  return JSON.stringify([parentId, profile]);
}
export function bucketKey(group: string, bucket: TeamBucket): string {
  return JSON.stringify([group, bucket]);
}

/** Group only direct children; a definition used by two parents has two groups. */
export class TeamGroups {
  private groups = new Map<string, string[]>();
  private buckets = new Map<string, TeamBucket>();
  private completed = new Map<string, number>();
  private sequence = 0;

  update(parentId: string, children: readonly TeamAgent[]): DefinitionGroup[] {
    const wave = ++this.sequence;
    const collected = new Map<string, DefinitionGroup>();
    const order = this.groups.get(parentId) || [];
    for (const child of children) {
      const key = definitionGroupKey(parentId, child.profile);
      if (!order.includes(key)) order.push(key);
      const group = collected.get(key) || { key, parentId, profile: child.profile, running: [], past: [] };
      const bucket = teamBucket(child), previous = this.buckets.get(child.id);
      if (bucket === "past" && previous === "running") this.completed.set(child.id, wave);
      this.buckets.set(child.id, bucket);
      group[bucket].push(child); collected.set(key, group);
    }
    this.groups.set(parentId, order);
    for (const group of collected.values()) group.past.sort((a, b) =>
      (this.completed.get(b.id) || 0) - (this.completed.get(a.id) || 0) ||
      (b.task?.completed_at || b.startedAt) - (a.task?.completed_at || a.startedAt) || a.id.localeCompare(b.id));
    return order.flatMap(key => { const group = collected.get(key); return group ? [group] : []; });
  }
}
